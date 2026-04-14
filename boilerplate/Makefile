obj-m += monitor.o

KDIR := /lib/modules/$(shell uname -r)/build
PWD  := $(shell pwd)

EXTRA_CFLAGS := -Wall

all: module engine cpu_hog io_pulse memory_hog

module:
	$(MAKE) -C $(KDIR) M=$(PWD) modules

engine: engine.c monitor_ioctl.h
	gcc -O2 -Wall -o engine engine.c -lpthread

cpu_hog: cpu_hog.c
	gcc -O2 -Wall -o cpu_hog cpu_hog.c

io_pulse: io_pulse.c
	gcc -O2 -Wall -o io_pulse io_pulse.c

memory_hog: memory_hog.c
	gcc -O2 -Wall -o memory_hog memory_hog.c

clean:
	$(MAKE) -C $(KDIR) M=$(PWD) clean
	rm -f engine cpu_hog io_pulse memory_hog
