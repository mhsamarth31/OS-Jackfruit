#!/usr/bin/env python3
"""
Run this script in your Ubuntu VM:
    cd ~/OS-Jackfruit/boilerplate
    python3 install.py

It will write all project files and build everything automatically.
"""

import os, sys, subprocess

BASE = os.path.expanduser("~/OS-Jackfruit/boilerplate")
os.makedirs(BASE, exist_ok=True)
os.chdir(BASE)

print("=" * 50)
print("  Writing project files...")
print("=" * 50)

# ─── engine.c ──────────────────────────────────────
ENGINE_C = r"""
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <signal.h>
#include <fcntl.h>
#include <time.h>
#include <pthread.h>
#include <sys/wait.h>
#include <sys/stat.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <sys/mount.h>
#include <sched.h>

#define MAX_CONTAINERS 16
#define LOG_BUFFER_SIZE 64
#define LOG_ENTRY_SIZE 512
#define SOCKET_PATH "/tmp/engine.sock"
#define LOG_DIR "/tmp/container_logs"

typedef enum { STATE_STARTING, STATE_RUNNING, STATE_STOPPED, STATE_KILLED } ContainerState;

typedef struct {
    char id[64];
    pid_t host_pid;
    time_t start_time;
    ContainerState state;
    long soft_limit_mb;
    long hard_limit_mb;
    char log_path[256];
    int exit_status;
    int log_pipe[2];
    int active;
} Container;

typedef struct {
    char entries[LOG_BUFFER_SIZE][LOG_ENTRY_SIZE];
    int head, tail, count;
    pthread_mutex_t mutex;
    pthread_cond_t not_empty;
    pthread_cond_t not_full;
} LogBuffer;

static Container containers[MAX_CONTAINERS];
static pthread_mutex_t containers_mutex = PTHREAD_MUTEX_INITIALIZER;
static LogBuffer log_buffer;
static volatile int supervisor_running = 1;
static pthread_t log_consumer_thread;

void log_buffer_init(void) {
    memset(&log_buffer, 0, sizeof(log_buffer));
    pthread_mutex_init(&log_buffer.mutex, NULL);
    pthread_cond_init(&log_buffer.not_empty, NULL);
    pthread_cond_init(&log_buffer.not_full, NULL);
}

void log_buffer_push(const char *entry) {
    pthread_mutex_lock(&log_buffer.mutex);
    while (log_buffer.count == LOG_BUFFER_SIZE)
        pthread_cond_wait(&log_buffer.not_full, &log_buffer.mutex);
    strncpy(log_buffer.entries[log_buffer.tail], entry, LOG_ENTRY_SIZE - 1);
    log_buffer.tail = (log_buffer.tail + 1) % LOG_BUFFER_SIZE;
    log_buffer.count++;
    pthread_cond_signal(&log_buffer.not_empty);
    pthread_mutex_unlock(&log_buffer.mutex);
}

int log_buffer_pop(char *out) {
    pthread_mutex_lock(&log_buffer.mutex);
    while (log_buffer.count == 0 && supervisor_running)
        pthread_cond_wait(&log_buffer.not_empty, &log_buffer.mutex);
    if (log_buffer.count == 0) { pthread_mutex_unlock(&log_buffer.mutex); return 0; }
    strncpy(out, log_buffer.entries[log_buffer.head], LOG_ENTRY_SIZE - 1);
    log_buffer.head = (log_buffer.head + 1) % LOG_BUFFER_SIZE;
    log_buffer.count--;
    pthread_cond_signal(&log_buffer.not_full);
    pthread_mutex_unlock(&log_buffer.mutex);
    return 1;
}

void *log_consumer(void *arg) {
    (void)arg;
    char entry[LOG_ENTRY_SIZE];
    while (supervisor_running || log_buffer.count > 0) {
        if (!log_buffer_pop(entry)) break;
        char *sep = strchr(entry, '|');
        if (!sep) continue;
        *sep = '\0';
        FILE *f = fopen(entry, "a");
        if (f) { fputs(sep + 1, f); fclose(f); }
    }
    return NULL;
}

void *log_producer(void *arg) {
    Container *c = (Container *)arg;
    char buf[512], entry[LOG_ENTRY_SIZE];
    ssize_t n;
    while ((n = read(c->log_pipe[0], buf, sizeof(buf) - 1)) > 0) {
        buf[n] = '\0';
        snprintf(entry, sizeof(entry), "%s|%s", c->log_path, buf);
        log_buffer_push(entry);
    }
    close(c->log_pipe[0]);
    return NULL;
}

static void sigchld_handler(int sig) {
    (void)sig;
    int status; pid_t pid;
    while ((pid = waitpid(-1, &status, WNOHANG)) > 0) {
        pthread_mutex_lock(&containers_mutex);
        for (int i = 0; i < MAX_CONTAINERS; i++) {
            if (containers[i].active && containers[i].host_pid == pid) {
                containers[i].exit_status = WEXITSTATUS(status);
                containers[i].state = STATE_STOPPED;
                break;
            }
        }
        pthread_mutex_unlock(&containers_mutex);
    }
}

static void sigterm_handler(int sig) {
    (void)sig;
    supervisor_running = 0;
    pthread_cond_broadcast(&log_buffer.not_empty);
}

int find_free_slot(void) {
    for (int i = 0; i < MAX_CONTAINERS; i++)
        if (!containers[i].active) return i;
    return -1;
}

int find_container(const char *id) {
    for (int i = 0; i < MAX_CONTAINERS; i++)
        if (containers[i].active && strcmp(containers[i].id, id) == 0) return i;
    return -1;
}

int launch_container(const char *id, const char *rootfs, const char *cmd,
                     int foreground, long soft_mb, long hard_mb) {
    pthread_mutex_lock(&containers_mutex);
    int slot = find_free_slot();
    if (slot < 0) { pthread_mutex_unlock(&containers_mutex); fprintf(stderr,"No free slots\n"); return -1; }
    Container *c = &containers[slot];
    memset(c, 0, sizeof(*c));
    strncpy(c->id, id, sizeof(c->id)-1);
    c->soft_limit_mb = soft_mb; c->hard_limit_mb = hard_mb;
    c->start_time = time(NULL); c->state = STATE_STARTING; c->active = 1;
    mkdir(LOG_DIR, 0755);
    snprintf(c->log_path, sizeof(c->log_path), LOG_DIR "/%s.log", id);
    if (pipe(c->log_pipe) < 0) { perror("pipe"); c->active=0; pthread_mutex_unlock(&containers_mutex); return -1; }
    pthread_mutex_unlock(&containers_mutex);

    pthread_t prod_tid;
    pthread_create(&prod_tid, NULL, log_producer, c);
    pthread_detach(prod_tid);

    pid_t pid = fork();
    if (pid == 0) {
        close(c->log_pipe[0]);
        dup2(c->log_pipe[1], STDOUT_FILENO);
        dup2(c->log_pipe[1], STDERR_FILENO);
        close(c->log_pipe[1]);
        unshare(CLONE_NEWUTS | CLONE_NEWNS);
        if (chroot(rootfs) < 0) { perror("chroot"); exit(1); }
        chdir("/");
        mount("proc", "/proc", "proc", 0, NULL);
        char hostname[128];
        snprintf(hostname, sizeof(hostname), "ctr-%s", id);
        sethostname(hostname, strlen(hostname));
        execl("/bin/sh", "sh", "-c", cmd, NULL);
        perror("execl"); exit(1);
    }
    if (pid < 0) { perror("fork"); containers[slot].active=0; return -1; }
    close(c->log_pipe[1]);
    pthread_mutex_lock(&containers_mutex);
    c->host_pid = pid; c->state = STATE_RUNNING;
    pthread_mutex_unlock(&containers_mutex);
    printf("[supervisor] Container '%s' started -- PID %d\n", id, pid);

    if (foreground) {
        int status;
        waitpid(pid, &status, 0);
        pthread_mutex_lock(&containers_mutex);
        c->state = STATE_STOPPED; c->exit_status = WEXITSTATUS(status);
        pthread_mutex_unlock(&containers_mutex);
    }
    return 0;
}

void cmd_ps(char *out, int out_size) {
    pthread_mutex_lock(&containers_mutex);
    int off = 0;
    off += snprintf(out+off, out_size-off, "\n%-16s %-8s %-10s %-10s %-10s\n",
                    "ID","PID","STATE","SOFT_MB","HARD_MB");
    off += snprintf(out+off, out_size-off, "%-16s %-8s %-10s %-10s %-10s\n",
                    "----------------","--------","----------","----------","----------");
    for (int i = 0; i < MAX_CONTAINERS; i++) {
        if (!containers[i].active) continue;
        const char *st[] = {"starting","running","stopped","killed"};
        off += snprintf(out+off, out_size-off, "%-16s %-8d %-10s %-10ld %-10ld\n",
                        containers[i].id, containers[i].host_pid,
                        st[containers[i].state],
                        containers[i].soft_limit_mb, containers[i].hard_limit_mb);
    }
    pthread_mutex_unlock(&containers_mutex);
}

void cmd_stop(const char *id) {
    pthread_mutex_lock(&containers_mutex);
    int idx = find_container(id);
    if (idx < 0) { pthread_mutex_unlock(&containers_mutex); return; }
    pid_t pid = containers[idx].host_pid;
    containers[idx].state = STATE_STOPPED;
    pthread_mutex_unlock(&containers_mutex);
    kill(pid, SIGTERM);
    printf("[supervisor] Stopped '%s' (PID %d)\n", id, pid);
}

void *cli_server(void *arg) {
    (void)arg;
    int sfd = socket(AF_UNIX, SOCK_STREAM, 0);
    struct sockaddr_un addr;
    memset(&addr, 0, sizeof(addr));
    addr.sun_family = AF_UNIX;
    strncpy(addr.sun_path, SOCKET_PATH, sizeof(addr.sun_path)-1);
    unlink(SOCKET_PATH);
    bind(sfd, (struct sockaddr *)&addr, sizeof(addr));
    listen(sfd, 5);
    while (supervisor_running) {
        int cfd = accept(sfd, NULL, NULL);
        if (cfd < 0) continue;
        char buf[512] = {0};
        read(cfd, buf, sizeof(buf)-1);
        char response[4096] = {0};
        char c1[64]={}, c2[128]={}, c3[256]={}, c4[256]={};
        int n = sscanf(buf, "%63s %127s %255s %255s", c1, c2, c3, c4);
        if (strcmp(c1,"ps")==0) {
            cmd_ps(response, sizeof(response));
        } else if (strcmp(c1,"stop")==0 && n>=2) {
            cmd_stop(c2);
            snprintf(response, sizeof(response), "Stopped %s\n", c2);
        } else if (strcmp(c1,"logs")==0 && n>=2) {
            pthread_mutex_lock(&containers_mutex);
            int idx = find_container(c2);
            char path[256]="";
            if (idx>=0) strncpy(path, containers[idx].log_path, 255);
            pthread_mutex_unlock(&containers_mutex);
            if (path[0]) {
                FILE *f = fopen(path,"r");
                if (f) { fread(response,1,sizeof(response)-1,f); fclose(f); }
                else snprintf(response,sizeof(response),"No log yet\n");
            } else snprintf(response,sizeof(response),"Container not found\n");
        } else if (strcmp(c1,"start")==0 && n>=3) {
            char runcmd[256]="echo hello from container";
            if (n>=4) strncpy(runcmd,c4,sizeof(runcmd)-1);
            launch_container(c2, c3, runcmd, 0, 128, 256);
            snprintf(response, sizeof(response), "Started %s\n", c2);
        } else {
            snprintf(response, sizeof(response), "Unknown: %s\n", c1);
        }
        write(cfd, response, strlen(response));
        close(cfd);
    }
    close(sfd); unlink(SOCKET_PATH);
    return NULL;
}

void run_supervisor(const char *rootfs) {
    printf("[supervisor] Started. rootfs=%s\n", rootfs);
    struct sigaction sa;
    memset(&sa,0,sizeof(sa));
    sa.sa_handler = sigchld_handler;
    sa.sa_flags = SA_RESTART|SA_NOCLDSTOP;
    sigaction(SIGCHLD,&sa,NULL);
    sa.sa_handler = sigterm_handler;
    sigaction(SIGTERM,&sa,NULL);
    sigaction(SIGINT,&sa,NULL);
    log_buffer_init();
    memset(containers, 0, sizeof(containers));
    pthread_create(&log_consumer_thread, NULL, log_consumer, NULL);
    pthread_t cli_tid;
    pthread_create(&cli_tid, NULL, cli_server, NULL);
    printf("[supervisor] Ready! Commands:\n");
    printf("  sudo ./engine start <id> <rootfs_path>\n");
    printf("  sudo ./engine ps\n");
    printf("  sudo ./engine stop <id>\n");
    printf("  sudo ./engine logs <id>\n");
    while (supervisor_running) {
        sleep(1);
        while (waitpid(-1, NULL, WNOHANG) > 0);
    }
    printf("[supervisor] Shutting down...\n");
    supervisor_running = 0;
    pthread_cond_broadcast(&log_buffer.not_empty);
    pthread_join(log_consumer_thread, NULL);
    pthread_cancel(cli_tid);
    printf("[supervisor] Clean exit.\n");
}

void send_command(const char *msg) {
    int fd = socket(AF_UNIX, SOCK_STREAM, 0);
    struct sockaddr_un addr;
    memset(&addr,0,sizeof(addr));
    addr.sun_family = AF_UNIX;
    strncpy(addr.sun_path, SOCKET_PATH, sizeof(addr.sun_path)-1);
    if (connect(fd,(struct sockaddr*)&addr,sizeof(addr))<0) {
        fprintf(stderr,"Cannot connect to supervisor. Is it running?\n"); exit(1);
    }
    write(fd, msg, strlen(msg));
    shutdown(fd, SHUT_WR);
    char buf[4096]={0}; ssize_t n;
    while ((n=read(fd,buf,sizeof(buf)-1))>0) fwrite(buf,1,n,stdout);
    close(fd);
}

int main(int argc, char *argv[]) {
    if (argc < 2) {
        printf("Usage:\n"
               "  sudo ./engine supervisor <rootfs>\n"
               "  sudo ./engine start <id> <rootfs>\n"
               "  sudo ./engine run   <id> <rootfs>\n"
               "  sudo ./engine ps\n"
               "  sudo ./engine stop  <id>\n"
               "  sudo ./engine logs  <id>\n");
        return 1;
    }
    if (strcmp(argv[1],"supervisor")==0) {
        if (argc<3){fprintf(stderr,"Need rootfs path\n");return 1;}
        run_supervisor(argv[2]);
    } else if (strcmp(argv[1],"start")==0) {
        if (argc<4){fprintf(stderr,"Need id and rootfs\n");return 1;}
        char msg[512];
        snprintf(msg,sizeof(msg),"start %s %s %s",argv[2],argv[3],argc>=5?argv[4]:"echo hello");
        send_command(msg);
    } else if (strcmp(argv[1],"run")==0) {
        if (argc<4){fprintf(stderr,"Need id and rootfs\n");return 1;}
        launch_container(argv[2],argv[3],argc>=5?argv[4]:"/bin/sh",1,128,256);
    } else if (strcmp(argv[1],"ps")==0) {
        send_command("ps");
    } else if (strcmp(argv[1],"stop")==0) {
        if (argc<3){fprintf(stderr,"Need id\n");return 1;}
        char msg[256]; snprintf(msg,sizeof(msg),"stop %s",argv[2]); send_command(msg);
    } else if (strcmp(argv[1],"logs")==0) {
        if (argc<3){fprintf(stderr,"Need id\n");return 1;}
        char msg[256]; snprintf(msg,sizeof(msg),"logs %s",argv[2]); send_command(msg);
    } else {
        fprintf(stderr,"Unknown command: %s\n",argv[1]); return 1;
    }
    return 0;
}
"""

# ─── monitor_ioctl.h ───────────────────────────────
MONITOR_IOCTL_H = """
#ifndef MONITOR_IOCTL_H
#define MONITOR_IOCTL_H

#include <linux/ioctl.h>

#define MONITOR_MAGIC 'M'

struct container_info {
    int  pid;
    char container_id[64];
    long soft_limit_mb;
    long hard_limit_mb;
};

#define IOCTL_REGISTER_CONTAINER   _IOW(MONITOR_MAGIC, 1, struct container_info)
#define IOCTL_UNREGISTER_CONTAINER _IOW(MONITOR_MAGIC, 2, struct container_info)

#endif
"""

# ─── monitor.c ─────────────────────────────────────
MONITOR_C = """
#include <linux/module.h>
#include <linux/kernel.h>
#include <linux/init.h>
#include <linux/fs.h>
#include <linux/cdev.h>
#include <linux/uaccess.h>
#include <linux/slab.h>
#include <linux/list.h>
#include <linux/mutex.h>
#include <linux/timer.h>
#include <linux/sched.h>
#include <linux/sched/signal.h>
#include <linux/mm.h>
#include <linux/pid.h>
#include "monitor_ioctl.h"

MODULE_LICENSE("GPL");
MODULE_AUTHOR("Student");
MODULE_DESCRIPTION("Container Memory Monitor LKM");

#define DEVICE_NAME "container_monitor"
#define CHECK_INTERVAL_MS 5000

static int major_number;
static struct cdev mon_cdev;
static struct class *mon_class;
static struct timer_list check_timer;

struct monitored_proc {
    pid_t pid;
    char  container_id[64];
    long  soft_limit_kb;
    long  hard_limit_kb;
    int   soft_warned;
    struct list_head list;
};

static LIST_HEAD(proc_list);
static DEFINE_MUTEX(proc_list_mutex);

static long get_rss_kb(pid_t pid)
{
    struct task_struct *task;
    long rss = 0;
    rcu_read_lock();
    task = pid_task(find_vpid(pid), PIDTYPE_PID);
    if (task && task->mm)
        rss = get_mm_rss(task->mm) * (PAGE_SIZE / 1024);
    rcu_read_unlock();
    return (task == NULL) ? -1 : rss;
}

static void check_memory(struct timer_list *t)
{
    struct monitored_proc *entry, *tmp;
    mutex_lock(&proc_list_mutex);
    list_for_each_entry_safe(entry, tmp, &proc_list, list) {
        long rss = get_rss_kb(entry->pid);
        if (rss < 0) {
            pr_info("container_monitor: PID %d gone, removing\\n", entry->pid);
            list_del(&entry->list); kfree(entry); continue;
        }
        if (rss > entry->hard_limit_kb) {
            struct task_struct *task;
            pr_warn("container_monitor: [%s] PID %d RSS %ldKB > hard %ldKB -- KILLING\\n",
                    entry->container_id, entry->pid, rss, entry->hard_limit_kb);
            rcu_read_lock();
            task = pid_task(find_vpid(entry->pid), PIDTYPE_PID);
            if (task) send_sig(SIGKILL, task, 0);
            rcu_read_unlock();
        } else if (rss > entry->soft_limit_kb && !entry->soft_warned) {
            pr_warn("container_monitor: [%s] PID %d RSS %ldKB > soft %ldKB -- WARNING\\n",
                    entry->container_id, entry->pid, rss, entry->soft_limit_kb);
            entry->soft_warned = 1;
        }
    }
    mutex_unlock(&proc_list_mutex);
    mod_timer(&check_timer, jiffies + msecs_to_jiffies(CHECK_INTERVAL_MS));
}

static long mon_ioctl(struct file *f, unsigned int cmd, unsigned long arg)
{
    struct container_info info;
    struct monitored_proc *entry, *tmp;
    switch (cmd) {
    case IOCTL_REGISTER_CONTAINER:
        if (copy_from_user(&info,(void __user *)arg,sizeof(info))) return -EFAULT;
        entry = kmalloc(sizeof(*entry), GFP_KERNEL);
        if (!entry) return -ENOMEM;
        entry->pid = info.pid;
        entry->soft_limit_kb = info.soft_limit_mb * 1024;
        entry->hard_limit_kb = info.hard_limit_mb * 1024;
        entry->soft_warned = 0;
        strncpy(entry->container_id, info.container_id, sizeof(entry->container_id)-1);
        INIT_LIST_HEAD(&entry->list);
        mutex_lock(&proc_list_mutex);
        list_add(&entry->list, &proc_list);
        mutex_unlock(&proc_list_mutex);
        pr_info("container_monitor: registered [%s] PID=%d\\n", info.container_id, info.pid);
        break;
    case IOCTL_UNREGISTER_CONTAINER:
        if (copy_from_user(&info,(void __user *)arg,sizeof(info))) return -EFAULT;
        mutex_lock(&proc_list_mutex);
        list_for_each_entry_safe(entry, tmp, &proc_list, list) {
            if (entry->pid == info.pid) { list_del(&entry->list); kfree(entry); break; }
        }
        mutex_unlock(&proc_list_mutex);
        pr_info("container_monitor: unregistered PID=%d\\n", info.pid);
        break;
    default: return -EINVAL;
    }
    return 0;
}

static int mon_open(struct inode *i, struct file *f)    { return 0; }
static int mon_release(struct inode *i, struct file *f) { return 0; }

static const struct file_operations mon_fops = {
    .owner = THIS_MODULE, .open = mon_open,
    .release = mon_release, .unlocked_ioctl = mon_ioctl,
};

static int __init mon_init(void)
{
    dev_t dev;
    alloc_chrdev_region(&dev, 0, 1, DEVICE_NAME);
    major_number = MAJOR(dev);
    cdev_init(&mon_cdev, &mon_fops);
    cdev_add(&mon_cdev, dev, 1);
    mon_class = class_create(DEVICE_NAME);
    device_create(mon_class, NULL, dev, NULL, DEVICE_NAME);
    timer_setup(&check_timer, check_memory, 0);
    mod_timer(&check_timer, jiffies + msecs_to_jiffies(CHECK_INTERVAL_MS));
    pr_info("container_monitor: loaded -- /dev/%s ready\\n", DEVICE_NAME);
    return 0;
}

static void __exit mon_exit(void)
{
    struct monitored_proc *e, *tmp;
    del_timer_sync(&check_timer);
    mutex_lock(&proc_list_mutex);
    list_for_each_entry_safe(e, tmp, &proc_list, list) { list_del(&e->list); kfree(e); }
    mutex_unlock(&proc_list_mutex);
    dev_t dev = MKDEV(major_number, 0);
    device_destroy(mon_class, dev); class_destroy(mon_class);
    cdev_del(&mon_cdev); unregister_chrdev_region(dev, 1);
    pr_info("container_monitor: unloaded\\n");
}

module_init(mon_init);
module_exit(mon_exit);
"""

# ─── Makefile ──────────────────────────────────────
MAKEFILE = """obj-m += monitor.o

KDIR := /lib/modules/$(shell uname -r)/build
PWD  := $(shell pwd)

EXTRA_CFLAGS := -Wall

all: module engine cpu_hog io_pulse memory_hog

module:
\t$(MAKE) -C $(KDIR) M=$(PWD) modules

engine: engine.c monitor_ioctl.h
\tgcc -O2 -Wall -o engine engine.c -lpthread

cpu_hog: cpu_hog.c
\tgcc -O2 -Wall -o cpu_hog cpu_hog.c

io_pulse: io_pulse.c
\tgcc -O2 -Wall -o io_pulse io_pulse.c

memory_hog: memory_hog.c
\tgcc -O2 -Wall -o memory_hog memory_hog.c

clean:
\t$(MAKE) -C $(KDIR) M=$(PWD) clean
\trm -f engine cpu_hog io_pulse memory_hog
"""

files = {
    "engine.c":        ENGINE_C.strip(),
    "monitor_ioctl.h": MONITOR_IOCTL_H.strip(),
    "monitor.c":       MONITOR_C.strip(),
    "Makefile":        MAKEFILE,
}

for name, content in files.items():
    path = os.path.join(BASE, name)
    with open(path, "w") as f:
        f.write(content)
    print(f"  [OK] wrote {path}")

print("\n[Building...]")
result = subprocess.run(["make"], cwd=BASE, capture_output=False)
if result.returncode == 0:
    print("\n" + "="*50)
    print("  BUILD SUCCESSFUL!")
    print("="*50)
    print("\nNext steps:")
    print("  sudo insmod monitor.ko")
    print("  ls -l /dev/container_monitor")
    print(f"  sudo ./engine supervisor {BASE}/../rootfs")
else:
    print("\nBuild had errors — send me a screenshot!")
