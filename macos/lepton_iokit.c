/*
 * lepton_iokit.c - PureThermal / FLIR Lepton access on macOS through IOKit.
 *
 * Why this exists: on macOS 26 (Apple Silicon), libuvc/libusb detaching Apple's
 * camera driver as root (a capture re-enumeration) caused kernel panics in
 * IOUSBHostFamily. This library instead follows the approach of
 * Lepton-GetThermal (https://github.com/hmenzagh/Lepton-GetThermal, MIT):
 *
 *   USBDeviceOpenSeize -> SetConfiguration(0), SetConfiguration(1)
 *   -> parse config descriptor (Y16 format, VS interface, iso endpoint)
 *   -> USBInterfaceOpenSeize(VideoStreaming) -> UVC probe/commit
 *   -> SetAlternateInterface -> ReadIsochPipeAsync on a CFRunLoop thread
 *
 * No root, no re-enumeration. Lepton SDK commands go over UVC extension-unit
 * control requests on the default pipe.
 *
 * Built automatically by lepton_iokit.py:
 *   clang -O2 -dynamiclib -framework IOKit -framework CoreFoundation \
 *         -o liblepton_iokit.dylib lepton_iokit.c
 */
#include <CoreFoundation/CoreFoundation.h>
#include <IOKit/IOCFPlugIn.h>
#include <IOKit/IOKitLib.h>
#include <IOKit/usb/IOUSBLib.h>
#include <pthread.h>
#include <stdarg.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#define PT_VID 0x1e4e
#define PT_PID 0x0100
#define FRAMES_PER_XFER 32      /* isochronous (1 ms) frames per request */
#define NUM_XFERS 4             /* requests in flight: 128 ms of queued reads */
#define START_LEAD 3            /* first read this many frames ahead of "now" */
#define MAX_PKT 3072            /* upper bound for one iso packet */

typedef void (*lt_frame_cb)(const uint8_t *data, int nbytes, int width, int height);

/* ---------------------------------------------------------------- state --- */
static IOUSBDeviceInterface320 **g_dev;
static IOUSBInterfaceInterface550 **g_vs;
static uint8_t g_vc_if, g_vs_if, g_fmt, g_frm, g_alt, g_ep, g_pipe;
static uint16_t g_w, g_h, g_maxpkt;
static uint32_t g_interval;
static char g_err[512];
static char g_product[128];   /* USB product string, e.g. "PureThermal (fw:v1.3.0)" */

static pthread_t g_thread;
static int g_thread_started;
static atomic_int g_running, g_inflight, g_dead;
static lt_frame_cb g_cb;
static UInt64 g_next_frame;

typedef struct {
    uint8_t buf[FRAMES_PER_XFER * MAX_PKT];
    IOUSBIsocFrame frames[FRAMES_PER_XFER];
} xfer_t;
/* Static on purpose: never freed, so a late completion can't touch freed memory. */
static xfer_t g_x[NUM_XFERS];

static uint8_t *g_frame;
static size_t g_flen, g_fexp;

/* Frames are handed to the caller on a separate delivery thread, so a slow
   callback (e.g. Python waiting for the GIL) can never delay resubmitting
   isochronous reads. Missed polls desync the PureThermal's stream. */
static uint8_t *g_ready;
static atomic_int g_ready_full, g_deliver_run;
static pthread_t g_dthread;
static int g_dthread_started;
static pthread_mutex_t g_dmu = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t g_dcv = PTHREAD_COND_INITIALIZER;
static atomic_uint g_gaps, g_dropped;
/* Clean stop: PureThermal firmware <= 1.3.0 keeps stale per-frame state if
   streaming is switched off mid-frame (fixed in 1.3.1, "Fix/uvc stream
   restart"), which makes the next session silent or scrambled. So lt_stop()
   asks the stream thread to switch to alt 0 right after an end-of-frame. */
static atomic_int g_stop_req, g_clean_stop;

/* stream thread start handshake */
static pthread_mutex_t g_smu = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t g_scv = PTHREAD_COND_INITIALIZER;
static int g_start_rc;   /* 1 pending, 0 ok, -1 failed */
static int g_last_fid = -1;
static atomic_uint g_ok, g_bad;
static atomic_uint g_ncb, g_npk, g_nempty, g_nstatus, g_neof;   /* diagnostics */
static IOReturn g_last_result, g_last_frstatus;
static unsigned g_last_act;
static uint8_t g_last_hdr[12];
static struct { uint16_t act, req, slot; uint8_t xfer; IOReturn st; uint8_t b[4]; } g_ring[24];
static int g_ring_n;

static void seterr(const char *fmt, ...) __attribute__((format(printf, 1, 2)));
static void seterr(const char *fmt, ...) {
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(g_err, sizeof g_err, fmt, ap);
    va_end(ap);
}

const char *lt_last_error(void) { return g_err; }

/* USB product name of the opened board; it carries the firmware version. */
const char *lt_product(void) { return g_product; }

/* ------------------------------------------------------ descriptor parse --- */
static const uint8_t Y16_GUID[16] = {0x59, 0x31, 0x36, 0x20, 0x00, 0x00, 0x10, 0x00,
                                     0x80, 0x00, 0x00, 0xAA, 0x00, 0x38, 0x9B, 0x71};

static int parse_config(const uint8_t *d, size_t n) {
    int in_vs = 0, have_fmt = 0, have_frm = 0, have_ep = 0, vs_if = -1;
    uint8_t cur_if = 0, cur_alt = 0;
    uint16_t best = 0;
    for (size_t p = 0; p + 1 < n;) {
        size_t len = d[p];
        if (len < 2 || p + len > n) break;
        uint8_t type = d[p + 1];
        if (type == 0x04 && len >= 9) {                      /* INTERFACE */
            cur_if = d[p + 2];
            cur_alt = d[p + 3];
            uint8_t cls = d[p + 5], sub = d[p + 6];
            if (cls == 0x0E && sub == 0x01) g_vc_if = cur_if; /* VideoControl */
            in_vs = (cls == 0x0E && sub == 0x02);             /* VideoStreaming */
        } else if (type == 0x24 && in_vs && len >= 3) {       /* CS_INTERFACE */
            uint8_t st = d[p + 2];
            if (st == 0x04 && len >= 27 && !have_fmt &&       /* FORMAT_UNCOMPRESSED */
                memcmp(&d[p + 5], Y16_GUID, 16) == 0) {
                g_fmt = d[p + 3];
                vs_if = cur_if;
                have_fmt = 1;
            } else if (st == 0x05 && len >= 26 && have_fmt && !have_frm) { /* FRAME */
                g_frm = d[p + 3];
                g_w = (uint16_t)(d[p + 5] | d[p + 6] << 8);
                g_h = (uint16_t)(d[p + 7] | d[p + 8] << 8);
                g_interval = (uint32_t)d[p + 21] | (uint32_t)d[p + 22] << 8 |
                             (uint32_t)d[p + 23] << 16 | (uint32_t)d[p + 24] << 24;
                have_frm = 1;
            }
        } else if (type == 0x05 && len >= 7 && in_vs && cur_alt > 0 &&
                   vs_if >= 0 && cur_if == (uint8_t)vs_if) {  /* ENDPOINT */
            uint8_t ep = d[p + 2], attr = d[p + 3];
            uint16_t raw = (uint16_t)(d[p + 4] | d[p + 5] << 8);
            uint16_t eff = (uint16_t)((raw & 0x7FF) * (((raw >> 11) & 3) + 1));
            if ((ep & 0x80) && (attr & 3) == 1 && eff > best && eff <= MAX_PKT) {
                best = eff;
                g_alt = cur_alt;
                g_ep = ep;
                have_ep = 1;
            }
        }
        p += len;
    }
    if (!have_fmt) { seterr("camera does not offer the Y16 (radiometric) format"); return -1; }
    if (!have_frm) { seterr("no Y16 frame descriptor"); return -1; }
    if (!have_ep)  { seterr("no isochronous video endpoint"); return -1; }
    g_vs_if = (uint8_t)vs_if;
    g_maxpkt = best;
    return 0;
}

/* -------------------------------------------------------- control pipe --- */
static int devreq(uint8_t type, uint8_t req, uint16_t val, uint16_t idx,
                  void *buf, uint16_t len, uint16_t *done) {
    if (!g_dev) { seterr("camera not open"); return -1; }
    IOUSBDevRequestTO r;
    memset(&r, 0, sizeof r);
    r.bmRequestType = type;
    r.bRequest = req;
    r.wValue = val;
    r.wIndex = idx;
    r.wLength = len;
    r.pData = buf;
    r.noDataTimeout = 1000;
    r.completionTimeout = 2000;
    IOReturn kr = (*g_dev)->DeviceRequestTO(g_dev, &r);
    if (kr != kIOReturnSuccess) {
        seterr("USB control request 0x%02x (wValue 0x%04x wIndex 0x%04x) failed: 0x%08x",
               req, val, idx, kr);
        return -1;
    }
    if (done) *done = (uint16_t)r.wLenDone;
    return 0;
}

#define REQ_IN  USBmakebmRequestType(kUSBIn, kUSBClass, kUSBInterface)
#define REQ_OUT USBmakebmRequestType(kUSBOut, kUSBClass, kUSBInterface)

int lt_get_ctrl(uint8_t unit, uint8_t ctrl, uint8_t *buf, uint16_t len) {
    uint16_t done = 0;
    if (devreq(REQ_IN, 0x81 /* GET_CUR */, (uint16_t)(ctrl << 8),
               (uint16_t)(unit << 8 | g_vc_if), buf, len, &done) < 0) return -1;
    return done;
}

int lt_set_ctrl(uint8_t unit, uint8_t ctrl, const uint8_t *buf, uint16_t len) {
    if (devreq(REQ_OUT, 0x01 /* SET_CUR */, (uint16_t)(ctrl << 8),
               (uint16_t)(unit << 8 | g_vc_if), (void *)buf, len, NULL) < 0) return -1;
    return len;
}

int lt_get_ctrl_len(uint8_t unit, uint8_t ctrl) {
    uint8_t b[2] = {0, 0};
    if (devreq(REQ_IN, 0x85 /* GET_LEN */, (uint16_t)(ctrl << 8),
               (uint16_t)(unit << 8 | g_vc_if), b, 2, NULL) < 0) return -1;
    return b[0] | b[1] << 8;
}

/* ----------------------------------------------------- frame assembler --- */
/* Exported for offline tests (lt_test_feed); used internally by the stream. */
void lt_feed(const uint8_t *pkt, size_t n) {
    if (n < 2 || !g_frame) return;
    uint8_t hl = pkt[0], fl = pkt[1];
    if (hl < 2 || hl > n) return;
    int fid = fl & 0x01;
    if (fl & 0x40) {                                   /* ERR */
        g_flen = 0;
        g_last_fid = fid;
        return;
    }
    if (g_last_fid >= 0 && fid != g_last_fid && g_flen) g_flen = 0;  /* new frame */
    g_last_fid = fid;
    size_t dl = n - hl;
    if (g_flen + dl > g_fexp) {                        /* too long: drop frame */
        g_flen = g_fexp + 1;
    } else if (g_flen <= g_fexp) {
        memcpy(g_frame + g_flen, pkt + hl, dl);
        g_flen += dl;
    }
    if (fl & 0x02) {                                   /* EOF */
        if (g_flen == g_fexp) {
            atomic_fetch_add(&g_ok, 1);
            if (!g_dthread_started) {                  /* offline tests: deliver inline */
                if (g_cb) g_cb(g_frame, (int)g_fexp, g_w, g_h);
            } else if (!atomic_load(&g_ready_full)) {
                memcpy(g_ready, g_frame, g_fexp);
                pthread_mutex_lock(&g_dmu);
                atomic_store(&g_ready_full, 1);
                pthread_cond_signal(&g_dcv);
                pthread_mutex_unlock(&g_dmu);
            } else {
                atomic_fetch_add(&g_dropped, 1);       /* caller still busy with the last one */
            }
        } else {
            atomic_fetch_add(&g_bad, 1);
        }
        g_flen = 0;
    }
}

int lt_test_setup(int w, int h, lt_frame_cb cb) {  /* offline tests only */
    g_w = (uint16_t)w;
    g_h = (uint16_t)h;
    g_fexp = (size_t)w * h * 2;
    free(g_frame);
    g_frame = malloc(g_fexp + MAX_PKT);
    g_flen = 0;
    g_last_fid = -1;
    g_cb = cb;
    atomic_store(&g_ok, 0);
    atomic_store(&g_bad, 0);
    return g_frame ? 0 : -1;
}

/* ------------------------------------------------------ iso streaming --- */
static void iso_cb(void *refcon, IOReturn result, void *arg0);

static void submit(int i) {
    xfer_t *x = &g_x[i];
    for (int f = 0; f < FRAMES_PER_XFER; f++) {
        x->frames[f].frReqCount = g_maxpkt;
        x->frames[f].frActCount = 0;
        x->frames[f].frStatus = 0;
    }
    UInt64 now = 0;
    AbsoluteTime at;
    (*g_vs)->GetBusFrameNumber(g_vs, &now, &at);
    if (g_next_frame < now + 2) {               /* fell behind: there will be a gap */
        atomic_fetch_add(&g_gaps, 1);
        g_next_frame = now + START_LEAD;
    }
    IOReturn kr = (*g_vs)->ReadIsochPipeAsync(g_vs, g_pipe, x->buf, g_next_frame,
                                              FRAMES_PER_XFER, x->frames, iso_cb,
                                              (void *)(intptr_t)i);
    if (kr == kIOReturnSuccess) {
        g_next_frame += FRAMES_PER_XFER;
        atomic_fetch_add(&g_inflight, 1);
    } else {
        fprintf(stderr, "[lepton_iokit] ReadIsochPipeAsync failed: 0x%08x\n", kr);
        if (atomic_load(&g_inflight) == 0) atomic_store(&g_dead, 1);
    }
}

static void iso_cb(void *refcon, IOReturn result, void *arg0) {
    (void)arg0;
    int i = (int)(intptr_t)refcon;
    atomic_fetch_sub(&g_inflight, 1);
    if (!atomic_load(&g_running)) return;       /* stopping: don't resubmit */
    if (result != kIOReturnSuccess && result != kIOReturnUnderrun &&
        result != kIOReturnIsoTooOld && result != kIOReturnIsoTooNew) {
        fprintf(stderr, "[lepton_iokit] iso transfer error 0x%08x\n", result);
    }
    xfer_t *x = &g_x[i];
    size_t off = 0;
    atomic_fetch_add(&g_ncb, 1);
    g_last_result = result;
    for (int f = 0; f < FRAMES_PER_XFER; f++) {
        IOUSBIsocFrame *fr = &x->frames[f];
        if (fr->frActCount > 0) {
            atomic_fetch_add(&g_npk, 1);
            g_last_frstatus = fr->frStatus;
            g_last_act = fr->frActCount;
            if (g_ring_n < 24) {   /* first 24 data packets of the session */
                g_ring[g_ring_n].act = fr->frActCount;
                g_ring[g_ring_n].req = fr->frReqCount;
                g_ring[g_ring_n].st = fr->frStatus;
                g_ring[g_ring_n].slot = (uint16_t)f;
                g_ring[g_ring_n].xfer = (uint8_t)i;
                memcpy(g_ring[g_ring_n].b, x->buf + off, 4);
                g_ring_n++;
            }
            memcpy(g_last_hdr, x->buf + off, fr->frActCount < 12 ? fr->frActCount : 12);
            if (x->buf[off + 1] & 0x02) atomic_fetch_add(&g_neof, 1);
        } else {
            atomic_fetch_add(&g_nempty, 1);
        }
        if (fr->frActCount > 0 &&
            (fr->frStatus == kIOReturnSuccess || fr->frStatus == kIOReturnUnderrun))
        {
            lt_feed(x->buf + off, fr->frActCount);
            uint8_t hl = x->buf[off], fl = x->buf[off + 1];
            if (atomic_load(&g_stop_req) && fr->frActCount >= 2 && hl >= 2 && hl <= 12 &&
                hl <= fr->frActCount && (fl & 0x02)) {
                /* end of frame: stop now, while the camera is between frames */
                (*g_vs)->SetAlternateInterface(g_vs, 0);
                atomic_store(&g_clean_stop, 1);
                atomic_store(&g_running, 0);
                return;                                /* don't resubmit */
            }
        }
        else if (fr->frActCount > 0)
            atomic_fetch_add(&g_nstatus, 1);
        off += g_maxpkt;
    }
    submit(i);
}

/* Diagnostics: callbacks, packets with data, empty slots, packets dropped for status, EOFs. */
int lt_debug_ring(char *out, int n) {
    int w = 0;
    for (int k = 0; k < g_ring_n && w < n; k++)
        w += snprintf(out + w, (size_t)(n - w), "x%u s%02u act=%u req=%u st=%08x %02x %02x %02x %02x\n",
                      g_ring[k].xfer, g_ring[k].slot, g_ring[k].act, g_ring[k].req, g_ring[k].st,
                      g_ring[k].b[0], g_ring[k].b[1], g_ring[k].b[2], g_ring[k].b[3]);
    return w;
}

int lt_debug(char *out, int n) {
    return snprintf(out, (size_t)n,
        "gaps=%u dropped=%u callbacks=%u packets=%u empty=%u bad_status=%u eof=%u frames_ok=%u frames_bad=%u "
        "inflight=%d dead=%d last_result=0x%08x last_frstatus=0x%08x last_len=%u "
        "last_hdr=%02x %02x %02x %02x %02x %02x %02x %02x %02x %02x %02x %02x flen=%zu",
        atomic_load(&g_gaps), atomic_load(&g_dropped), atomic_load(&g_ncb), atomic_load(&g_npk), atomic_load(&g_nempty), atomic_load(&g_nstatus),
        atomic_load(&g_neof), atomic_load(&g_ok), atomic_load(&g_bad), atomic_load(&g_inflight),
        atomic_load(&g_dead), g_last_result, g_last_frstatus, g_last_act,
        g_last_hdr[0], g_last_hdr[1], g_last_hdr[2], g_last_hdr[3], g_last_hdr[4], g_last_hdr[5],
        g_last_hdr[6], g_last_hdr[7], g_last_hdr[8], g_last_hdr[9], g_last_hdr[10], g_last_hdr[11],
        g_flen);
}

static void start_done(int rc) {
    pthread_mutex_lock(&g_smu);
    g_start_rc = rc;
    pthread_cond_broadcast(&g_scv);
    pthread_mutex_unlock(&g_smu);
}

static void *deliver_main(void *arg) {
    (void)arg;
    for (;;) {
        pthread_mutex_lock(&g_dmu);
        while (!atomic_load(&g_ready_full) && atomic_load(&g_deliver_run))
            pthread_cond_wait(&g_dcv, &g_dmu);
        pthread_mutex_unlock(&g_dmu);
        if (!atomic_load(&g_deliver_run)) break;
        lt_frame_cb cb = g_cb;
        if (cb) cb(g_ready, (int)g_fexp, g_w, g_h);
        atomic_store(&g_ready_full, 0);
    }
    return NULL;
}

static void *stream_main(void *arg) {
    (void)arg;
    CFRunLoopSourceRef src = NULL;
    if ((*g_vs)->CreateInterfaceAsyncEventSource(g_vs, &src) != kIOReturnSuccess || !src) {
        seterr("could not create the USB event source");
        atomic_store(&g_dead, 1);
        start_done(-1);
        return NULL;
    }
    CFRunLoopRef rl = CFRunLoopGetCurrent();
    CFRunLoopAddSource(rl, src, kCFRunLoopDefaultMode);

    /* Only now switch the camera to streaming, so reads are queued within a
       few ms of it starting to send (missed polls desync its firmware). */
    IOReturn kr = (*g_vs)->SetAlternateInterface(g_vs, g_alt);
    if (kr != kIOReturnSuccess) {
        seterr("SetAlternateInterface(%u) failed: 0x%08x", g_alt, kr);
        goto fail;
    }
    UInt8 neps = 0;
    g_pipe = 0;
    (*g_vs)->GetNumEndpoints(g_vs, &neps);
    for (UInt8 i = 1; i <= neps; i++) {
        UInt8 dir, num, tt, interval;
        UInt16 mps;
        if ((*g_vs)->GetPipeProperties(g_vs, i, &dir, &num, &tt, &mps, &interval) == kIOReturnSuccess &&
            (uint8_t)(num | dir << 7) == g_ep) {
            g_pipe = i;
            break;
        }
    }
    if (!g_pipe) {
        seterr("video endpoint 0x%02x not found", g_ep);
        (*g_vs)->SetAlternateInterface(g_vs, 0);
        goto fail;
    }

    UInt64 now = 0;
    AbsoluteTime at;
    (*g_vs)->GetBusFrameNumber(g_vs, &now, &at);
    g_next_frame = now + START_LEAD;
    for (int i = 0; i < NUM_XFERS; i++) submit(i);
    if (atomic_load(&g_inflight) == 0) {
        seterr("could not queue any USB reads");
        (*g_vs)->SetAlternateInterface(g_vs, 0);
        goto fail;
    }
    start_done(0);

    int drain = 0;
    /* Run until stopped, then let in-flight requests finish (max ~2 s). */
    while (atomic_load(&g_running) || (atomic_load(&g_inflight) > 0 && drain++ < 40))
        CFRunLoopRunInMode(kCFRunLoopDefaultMode, 0.05, false);

    CFRunLoopRemoveSource(rl, src, kCFRunLoopDefaultMode);
    CFRelease(src);
    return NULL;

fail:
    atomic_store(&g_running, 0);
    CFRunLoopRemoveSource(rl, src, kCFRunLoopDefaultMode);
    CFRelease(src);
    start_done(-1);
    return NULL;
}

/* -------------------------------------------------------------- public --- */
static io_service_t find_device(void) {
    CFMutableDictionaryRef m = IOServiceMatching(kIOUSBDeviceClassName);
    if (!m) return 0;
    int32_t vid = PT_VID, pid = PT_PID;
    CFNumberRef v = CFNumberCreate(NULL, kCFNumberSInt32Type, &vid);
    CFNumberRef p = CFNumberCreate(NULL, kCFNumberSInt32Type, &pid);
    CFDictionarySetValue(m, CFSTR(kUSBVendorID), v);
    CFDictionarySetValue(m, CFSTR(kUSBProductID), p);
    CFRelease(v);
    CFRelease(p);
    io_iterator_t it = 0;
    if (IOServiceGetMatchingServices(kIOMainPortDefault, m, &it) != KERN_SUCCESS) return 0;
    io_service_t s = IOIteratorNext(it);
    IOObjectRelease(it);
    return s;
}

static IOUSBInterfaceInterface550 **find_vs_interface(void) {
    IOUSBFindInterfaceRequest req = {0x0E, 0x02, kIOUSBFindInterfaceDontCare,
                                     kIOUSBFindInterfaceDontCare};
    for (int attempt = 0; attempt < 20; attempt++) {  /* interfaces appear after SetConfiguration */
        io_iterator_t it = 0;
        if ((*g_dev)->CreateInterfaceIterator(g_dev, &req, &it) == KERN_SUCCESS) {
            io_service_t s = IOIteratorNext(it);
            IOObjectRelease(it);
            if (s) {
                IOCFPlugInInterface **plug = NULL;
                SInt32 score = 0;
                kern_return_t kr = IOCreatePlugInInterfaceForService(
                    s, kIOUSBInterfaceUserClientTypeID, kIOCFPlugInInterfaceID, &plug, &score);
                IOObjectRelease(s);
                if (kr != KERN_SUCCESS || !plug) {
                    seterr("could not access the video interface: 0x%08x", kr);
                    return NULL;
                }
                IOUSBInterfaceInterface550 **intf = NULL;
                (*plug)->QueryInterface(plug, CFUUIDGetUUIDBytes(kIOUSBInterfaceInterfaceID550),
                                        (LPVOID *)&intf);
                (*plug)->Release(plug);
                if (!intf) seterr("IOUSBInterfaceInterface550 unavailable");
                return intf;
            }
        }
        usleep(50 * 1000);
    }
    seterr("VideoStreaming interface not found");
    return NULL;
}

void lt_close(void);
void lt_stop(void);

int lt_open(void) {
    if (g_dev) return 0;
    g_err[0] = 0;
    io_service_t svc = find_device();
    if (!svc) { seterr("PureThermal not found (USB 1e4e:0100). Check the cable."); return -1; }

    g_product[0] = 0;   /* read from the registry: no USB traffic */
    CFTypeRef name = IORegistryEntryCreateCFProperty(svc, CFSTR(kUSBProductString), NULL, 0);
    if (name) {
        if (CFGetTypeID(name) == CFStringGetTypeID())
            CFStringGetCString((CFStringRef)name, g_product, sizeof g_product, kCFStringEncodingUTF8);
        CFRelease(name);
    }

    IOCFPlugInInterface **plug = NULL;
    SInt32 score = 0;
    kern_return_t kr = IOCreatePlugInInterfaceForService(
        svc, kIOUSBDeviceUserClientTypeID, kIOCFPlugInInterfaceID, &plug, &score);
    IOObjectRelease(svc);
    if (kr != KERN_SUCCESS || !plug) { seterr("could not access the USB device: 0x%08x", kr); return -1; }
    (*plug)->QueryInterface(plug, CFUUIDGetUUIDBytes(kIOUSBDeviceInterfaceID320), (LPVOID *)&g_dev);
    (*plug)->Release(plug);
    if (!g_dev) { seterr("IOUSBDeviceInterface320 unavailable"); return -1; }

    kr = (*g_dev)->USBDeviceOpenSeize(g_dev);
    if (kr != kIOReturnSuccess) {
        (*g_dev)->Release(g_dev);
        g_dev = NULL;
        seterr("could not open the camera (0x%08x). Quit other apps using it "
               "(FaceTime, Photo Booth, OBS, Zoom, browser tabs) and try again.", kr);
        return -1;
    }
    (*g_dev)->SetConfiguration(g_dev, 0);
    kr = (*g_dev)->SetConfiguration(g_dev, 1);
    if (kr != kIOReturnSuccess)
        fprintf(stderr, "[lepton_iokit] SetConfiguration(1): 0x%08x (may already be set)\n", kr);

    IOUSBConfigurationDescriptorPtr cd = NULL;
    kr = (*g_dev)->GetConfigurationDescriptorPtr(g_dev, 0, &cd);
    if (kr != kIOReturnSuccess || !cd) { seterr("could not read USB descriptors: 0x%08x", kr); lt_close(); return -1; }
    if (parse_config((const uint8_t *)cd, USBToHostWord(cd->wTotalLength)) < 0) { lt_close(); return -1; }

    g_vs = find_vs_interface();
    if (!g_vs) { lt_close(); return -1; }
    kr = (*g_vs)->USBInterfaceOpenSeize(g_vs);
    if (kr != kIOReturnSuccess) {
        (*g_vs)->Release(g_vs);
        g_vs = NULL;
        seterr("could not open the video interface: 0x%08x", kr);
        lt_close();
        return -1;
    }

    g_fexp = (size_t)g_w * g_h * 2;
    free(g_frame);
    g_frame = malloc(g_fexp + MAX_PKT);
    if (!g_frame) { seterr("out of memory"); lt_close(); return -1; }
    fprintf(stderr, "[lepton_iokit] opened: Y16 %ux%u fmt=%u frame=%u alt=%u ep=0x%02x pkt=%u vc_if=%u vs_if=%u\n",
            g_w, g_h, g_fmt, g_frm, g_alt, g_ep, g_maxpkt, g_vc_if, g_vs_if);
    return 0;
}

int lt_size(int *w, int *h) {
    if (!g_dev) return -1;
    *w = g_w;
    *h = g_h;
    return 0;
}

int lt_start(lt_frame_cb cb) {
    if (!g_dev || !g_vs) { seterr("camera not open"); return -1; }
    if (g_thread_started) return 0;

    uint8_t probe[26];
    memset(probe, 0, sizeof probe);
    probe[0] = 0x01;                               /* bmHint: keep dwFrameInterval */
    probe[2] = g_fmt;
    probe[3] = g_frm;
    memcpy(&probe[4], &g_interval, 4);             /* little-endian host */
    uint32_t maxframe = (uint32_t)g_fexp;
    memcpy(&probe[18], &maxframe, 4);
    if (devreq(REQ_OUT, 0x01, 0x0100, g_vs_if, probe, 26, NULL) < 0) return -1;  /* PROBE SET */
    if (devreq(REQ_IN, 0x81, 0x0100, g_vs_if, probe, 26, NULL) < 0) return -1;   /* PROBE GET */
    {
        uint32_t iv, mf, mp;
        memcpy(&iv, &probe[4], 4);
        memcpy(&mf, &probe[18], 4);
        memcpy(&mp, &probe[22], 4);
        fprintf(stderr, "[lepton_iokit] probe: fmt=%u frame=%u interval=%u max_frame=%u max_payload=%u\n",
                probe[2], probe[3], iv, mf, mp);
    }
    if (devreq(REQ_OUT, 0x01, 0x0200, g_vs_if, probe, 26, NULL) < 0) return -1;  /* COMMIT */

    if (!g_ready) g_ready = malloc(g_fexp);
    if (!g_ready) { seterr("out of memory"); return -1; }
    g_cb = cb;
    g_flen = 0;
    g_last_fid = -1;
    atomic_store(&g_ready_full, 0);
    atomic_store(&g_deliver_run, 1);
    if (pthread_create(&g_dthread, NULL, deliver_main, NULL) != 0) {
        seterr("could not start the delivery thread");
        return -1;
    }
    g_dthread_started = 1;

    atomic_store(&g_dead, 0);
    atomic_store(&g_inflight, 0);
    atomic_store(&g_stop_req, 0);
    atomic_store(&g_clean_stop, 0);
    atomic_store(&g_running, 1);
    g_start_rc = 1;
    if (pthread_create(&g_thread, NULL, stream_main, NULL) != 0) {
        atomic_store(&g_running, 0);
        seterr("could not start the streaming thread");
        lt_stop();
        return -1;
    }
    g_thread_started = 1;
    pthread_mutex_lock(&g_smu);
    while (g_start_rc == 1) pthread_cond_wait(&g_scv, &g_smu);
    int rc = g_start_rc;
    pthread_mutex_unlock(&g_smu);
    if (rc < 0) {
        char keep[sizeof g_err];
        memcpy(keep, g_err, sizeof keep);
        lt_stop();
        memcpy(g_err, keep, sizeof keep);
        return -1;
    }
    return 0;
}

void lt_stop(void) {
    if (g_thread_started) {
        /* Ask for a stop at the next end-of-frame (frames arrive every ~111 ms). */
        atomic_store(&g_stop_req, 1);
        for (int t = 0; t < 80 && atomic_load(&g_running); t++) usleep(5000);
        atomic_store(&g_running, 0);    /* no frame boundary seen: stop anyway */
        pthread_join(g_thread, NULL);   /* returns once in-flight requests finish */
        g_thread_started = 0;
        if (g_vs && !atomic_load(&g_clean_stop))
            (*g_vs)->SetAlternateInterface(g_vs, 0);   /* release iso bandwidth */
    }
    if (g_dthread_started) {
        pthread_mutex_lock(&g_dmu);
        atomic_store(&g_deliver_run, 0);
        pthread_cond_broadcast(&g_dcv);
        pthread_mutex_unlock(&g_dmu);
        pthread_join(g_dthread, NULL);
        g_dthread_started = 0;
    }
    g_cb = NULL;
}

void lt_close(void) {
    lt_stop();
    if (g_vs) {
        (*g_vs)->USBInterfaceClose(g_vs);
        (*g_vs)->Release(g_vs);
        g_vs = NULL;
    }
    if (g_dev) {
        (*g_dev)->USBDeviceClose(g_dev);
        (*g_dev)->Release(g_dev);
        g_dev = NULL;
    }
}

int lt_last_stop_clean(void) { return atomic_load(&g_clean_stop); }

/* frames_ok, frames_bad, stream_dead (1 if no transfers are running) */
void lt_stats(unsigned *ok, unsigned *bad, int *dead) {
    *ok = atomic_load(&g_ok);
    *bad = atomic_load(&g_bad);
    *dead = atomic_load(&g_dead);
}
