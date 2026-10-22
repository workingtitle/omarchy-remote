#define _GNU_SOURCE
#include <wayland-client.h>
#include <xkbcommon/xkbcommon.h>
#include <sys/mman.h>
#include <poll.h>
#include <unistd.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <signal.h>
#include <math.h>
#include "virtual-keyboard.h"
#include "virtual-pointer.h"

/* User-session helper. stdin is private to the authenticated controller.
 * No socket, shell execution, root permission or access to physical devices. */
static struct wl_display *display;
static struct wl_seat *seat;
static struct zwp_virtual_keyboard_manager_v1 *km;
static struct zwlr_virtual_pointer_manager_v1 *pm;
static struct zwp_virtual_keyboard_v1 *keyboard;
static struct zwlr_virtual_pointer_v1 *pointer;
static struct xkb_state *state;
static unsigned char keys[256], buttons[3];
static int armed;
static volatile sig_atomic_t exiting;
static uint32_t now_ms(void) { struct timespec t; clock_gettime(CLOCK_MONOTONIC,&t); return (uint32_t)(t.tv_sec*1000+t.tv_nsec/1000000); }
static void signal_exit(int sig) { (void)sig; exiting=1; }
static void global(void *data,struct wl_registry *r,uint32_t name,const char *interface,uint32_t version) {
    (void)data;(void)version;
    if (!strcmp(interface,"wl_seat")&&!seat) seat=wl_registry_bind(r,name,&wl_seat_interface,1);
    if (!strcmp(interface,"zwp_virtual_keyboard_manager_v1")) km=wl_registry_bind(r,name,&zwp_virtual_keyboard_manager_v1_interface,1);
    if (!strcmp(interface,"zwlr_virtual_pointer_manager_v1")) pm=wl_registry_bind(r,name,&zwlr_virtual_pointer_manager_v1_interface,1);
}
static void removed(void *data,struct wl_registry *r,uint32_t name) { (void)data;(void)r;(void)name; }
static void modifiers(void) {
    zwp_virtual_keyboard_v1_modifiers(keyboard,
        xkb_state_serialize_mods(state,XKB_STATE_MODS_DEPRESSED),
        xkb_state_serialize_mods(state,XKB_STATE_MODS_LATCHED),
        xkb_state_serialize_mods(state,XKB_STATE_MODS_LOCKED),
        xkb_state_serialize_layout(state,XKB_STATE_LAYOUT_EFFECTIVE));
}
static void key(unsigned code,unsigned down) {
    if (keys[code]==down) return;
    keys[code]=down;
    zwp_virtual_keyboard_v1_key(keyboard,now_ms(),code,down);
    xkb_state_update_key(state,code+8,down?XKB_KEY_DOWN:XKB_KEY_UP);
    modifiers();
}
static void release(void) {
    for (unsigned i=1;i<256;i++) if(keys[i]) key(i,0);
    for (unsigned i=0;i<3;i++) if(buttons[i]) { zwlr_virtual_pointer_v1_button(pointer,now_ms(),272+i,0);buttons[i]=0; }
    zwlr_virtual_pointer_v1_frame(pointer); wl_display_flush(display); armed=0;
}
static int command(const char *line) {
    unsigned a,b; double x,y; char extra;
    if (!strcmp(line,"release")||!strcmp(line,"capture 0")) { release();return 0; }
    if (!strcmp(line,"capture 1")) { release();armed=1;return 0; }
    if (!strcmp(line,"heartbeat")) return 0;
    if (!armed) return -1;
    if (sscanf(line,"key %u %u %c",&a,&b,&extra)==2&&a>0&&a<256&&b<2) key(a,b);
    else if (sscanf(line,"button %u %u %c",&a,&b,&extra)==2&&a>=272&&a<=274&&b<2) {
        buttons[a-272]=b;zwlr_virtual_pointer_v1_button(pointer,now_ms(),a,b);
    } else if (sscanf(line,"absolute %u %u %c",&a,&b,&extra)==2&&a<=65535&&b<=65535) {
        zwlr_virtual_pointer_v1_motion_absolute(pointer,now_ms(),a,b,65535,65535);
    } else if (sscanf(line,"relative %lf %lf %c",&x,&y,&extra)==2&&isfinite(x)&&isfinite(y)&&fabs(x)<=8192&&fabs(y)<=8192) {
        zwlr_virtual_pointer_v1_motion(pointer,now_ms(),wl_fixed_from_double(x),wl_fixed_from_double(y));
    } else if (sscanf(line,"scroll %lf %lf %c",&x,&y,&extra)==2&&isfinite(x)&&isfinite(y)&&fabs(x)<=8192&&fabs(y)<=8192) {
        zwlr_virtual_pointer_v1_axis_source(pointer,WL_POINTER_AXIS_SOURCE_CONTINUOUS);
        if(x) zwlr_virtual_pointer_v1_axis(pointer,now_ms(),WL_POINTER_AXIS_HORIZONTAL_SCROLL,wl_fixed_from_double(x));
        if(y) zwlr_virtual_pointer_v1_axis(pointer,now_ms(),WL_POINTER_AXIS_VERTICAL_SCROLL,wl_fixed_from_double(y));
    } else return -1;
    zwlr_virtual_pointer_v1_frame(pointer); return wl_display_flush(display)<0?-1:0;
}
int main(int argc,char **argv) {
    signal(SIGINT,signal_exit);signal(SIGTERM,signal_exit);signal(SIGHUP,signal_exit);
    display=wl_display_connect(NULL);if(!display){fputs("No Wayland session\n",stderr);return 1;}
    struct wl_registry *registry=wl_display_get_registry(display);
    struct wl_registry_listener listener={global,removed};wl_registry_add_listener(registry,&listener,NULL);wl_display_roundtrip(display);
    if(!seat||!km||!pm){fputs("Required virtual input protocols unavailable\n",stderr);return 1;}
    struct xkb_context *context=xkb_context_new(XKB_CONTEXT_NO_FLAGS);
    struct xkb_rule_names rules={.layout=argc>1?argv[1]:"us",.variant=argc>2?argv[2]:"",.options=argc>3?argv[3]:""};
    struct xkb_keymap *map=xkb_keymap_new_from_names(context,&rules,XKB_KEYMAP_COMPILE_NO_FLAGS);
    if(!map){fputs("Invalid keymap\n",stderr);return 1;}
    state=xkb_state_new(map);
    char *text=xkb_keymap_get_as_string(map,XKB_KEYMAP_FORMAT_TEXT_V1);
    int fd=memfd_create("omarchy-remote-keymap",MFD_CLOEXEC);size_t size=strlen(text)+1;
    if(fd<0||write(fd,text,size)!=(ssize_t)size){perror("keymap");return 1;}
    keyboard=zwp_virtual_keyboard_manager_v1_create_virtual_keyboard(km,seat);
    pointer=zwlr_virtual_pointer_manager_v1_create_virtual_pointer(pm,seat);
    zwp_virtual_keyboard_v1_keymap(keyboard,WL_KEYBOARD_KEYMAP_FORMAT_XKB_V1,fd,size);
    close(fd);free(text);wl_display_roundtrip(display);
    fputs("READY virtual keyboard + pointer\n",stderr);
    char buffer[256];size_t used=0;uint32_t last=now_ms();int result=0;
    while(!exiting) {
        struct pollfd fds[2]={{STDIN_FILENO,POLLIN,0},{wl_display_get_fd(display),POLLIN,0}};
        int ready=poll(fds,2,100);
        if(armed&&(uint32_t)(now_ms()-last)>1000) release();
        if(ready<0) {if(exiting)break;result=1;break;}
        if(fds[1].revents&(POLLERR|POLLHUP)) {result=1;break;}
        if(fds[1].revents&POLLIN) {if(wl_display_dispatch(display)<0){result=1;break;}}
        if(fds[0].revents&(POLLIN|POLLHUP)) {
            char byte;ssize_t n=read(STDIN_FILENO,&byte,1);if(n<=0)break;
            if(byte=='\n') {buffer[used]=0;if(command(buffer)<0){result=1;break;}used=0;last=now_ms();}
            else if(used<sizeof(buffer)-1)buffer[used++]=byte;
            else {result=1;break;}
        }
    }
    release();zwp_virtual_keyboard_v1_destroy(keyboard);zwlr_virtual_pointer_v1_destroy(pointer);wl_display_flush(display);
    xkb_state_unref(state);xkb_keymap_unref(map);xkb_context_unref(context);wl_display_disconnect(display);
    return result;
}
