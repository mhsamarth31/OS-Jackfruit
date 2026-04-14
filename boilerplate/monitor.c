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
#include <linux/timex.h>
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
            pr_info("container_monitor: PID %d gone, removing\n", entry->pid);
            list_del(&entry->list); kfree(entry); continue;
        }
        if (rss > entry->hard_limit_kb) {
            struct task_struct *task;
            pr_warn("container_monitor: [%s] PID %d RSS %ldKB > hard %ldKB -- KILLING\n",
                    entry->container_id, entry->pid, rss, entry->hard_limit_kb);
            rcu_read_lock();
            task = pid_task(find_vpid(entry->pid), PIDTYPE_PID);
            if (task) send_sig(SIGKILL, task, 0);
            rcu_read_unlock();
        } else if (rss > entry->soft_limit_kb && !entry->soft_warned) {
            pr_warn("container_monitor: [%s] PID %d RSS %ldKB > soft %ldKB -- WARNING\n",
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
        pr_info("container_monitor: registered [%s] PID=%d\n", info.container_id, info.pid);
        break;
    case IOCTL_UNREGISTER_CONTAINER:
        if (copy_from_user(&info,(void __user *)arg,sizeof(info))) return -EFAULT;
        mutex_lock(&proc_list_mutex);
        list_for_each_entry_safe(entry, tmp, &proc_list, list) {
            if (entry->pid == info.pid) { list_del(&entry->list); kfree(entry); break; }
        }
        mutex_unlock(&proc_list_mutex);
        pr_info("container_monitor: unregistered PID=%d\n", info.pid);
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
    pr_info("container_monitor: loaded -- /dev/%s ready\n", DEVICE_NAME);
    return 0;
}

static void __exit mon_exit(void)
{
    struct monitored_proc *e, *tmp;
    timer_shutdown_sync(&check_timer);
    mutex_lock(&proc_list_mutex);
    list_for_each_entry_safe(e, tmp, &proc_list, list) { list_del(&e->list); kfree(e); }
    mutex_unlock(&proc_list_mutex);
    dev_t dev = MKDEV(major_number, 0);
    device_destroy(mon_class, dev); class_destroy(mon_class);
    cdev_del(&mon_cdev); unregister_chrdev_region(dev, 1);
    pr_info("container_monitor: unloaded\n");
}

module_init(mon_init);
module_exit(mon_exit);