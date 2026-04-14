#OS Mini Project - Multi Container Runtime
## Team Information 
	Name:Samarth M H    Srn:PES1UG24CS407
	Name:Sampath	    srn:PES1UG24CS416
## BUild and Run Instructions

### Build 
cd boilerplate
make

###Load kernel module
sudo insmod monitor.ko
ls -l /dev/container_monitor

###Start supervisor 
sudo ./engine supervisor ../rootfs


###Start containers
sudo ./engine start alpha ../rootfs
sudo ./tngine start beta ..rootfs

###List containers
sudo ./engine ps

#view logs
sudo ./engine logs alpha


###Stop containers
sudo ./engine stop alpha
sudo ./engine stop beta

###Unload module 
sudo rmmod monitor 
dmesg |tail
