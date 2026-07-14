/*
 * nolock.c — LD_PRELOAD shim that strips MAP_LOCKED from every mmap() and turns
 * mlock()/mlockall() into no-ops.
 *
 * WHY: ZisK's ASM proving backend mmaps large shared-memory regions (ROM in the C
 * microservice, and multi-GB trace/output buffers in the Rust asm-runner inside
 * cargo-zisk) with MAP_LOCKED. On unprivileged Docker boxes (vast.ai) RLIMIT_MEMLOCK
 * is hard-capped (~64 KB) and CANNOT be raised (no CAP_SYS_RESOURCE), so those locked
 * mmaps fail with errno 11 (EAGAIN): "mmap(rom) / mmap(MAP_FIXED) ... Resource
 * temporarily unavailable". The `-u/--unlock-mapped-memory` CLI flag that would clear
 * MAP_LOCKED does NOT propagate through the SDK to the runner, so we strip the flag at
 * the syscall boundary for cargo-zisk and all its children. Unlocking is harmless with
 * hundreds of GB of RAM — the pages simply become swappable (and never actually swap).
 *
 *   gcc -shared -fPIC -O2 -o nolock.so nolock.c -ldl
 *   LD_PRELOAD=/path/nolock.so cargo-zisk prove --asm ...
 */
#define _GNU_SOURCE
#include <sys/mman.h>
#include <dlfcn.h>
#include <stddef.h>
#include <sys/types.h>

void *mmap(void *addr, size_t length, int prot, int flags, int fd, off_t offset) {
    static void *(*real)(void *, size_t, int, int, int, off_t) = NULL;
    if (!real) real = dlsym(RTLD_NEXT, "mmap");
    return real(addr, length, prot, flags & ~MAP_LOCKED, fd, offset);
}

void *mmap64(void *addr, size_t length, int prot, int flags, int fd, off_t offset) {
    static void *(*real)(void *, size_t, int, int, int, off_t) = NULL;
    if (!real) real = dlsym(RTLD_NEXT, "mmap64");
    return real(addr, length, prot, flags & ~MAP_LOCKED, fd, offset);
}

int mlock(const void *addr, size_t len)                 { (void)addr; (void)len; return 0; }
int mlock2(const void *addr, size_t len, unsigned int f){ (void)addr; (void)len; (void)f; return 0; }
int mlockall(int flags)                                 { (void)flags; return 0; }
