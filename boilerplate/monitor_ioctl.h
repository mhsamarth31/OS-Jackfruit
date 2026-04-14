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