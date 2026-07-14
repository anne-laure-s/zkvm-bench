/* Validates the nolock.so shim: tries to mmap 6 GB with MAP_LOCKED.
 * Without the shim this fails with errno 11 on a memlock-capped box;
 * with LD_PRELOAD=nolock.so it succeeds (MAP_LOCKED stripped). */
#include <sys/mman.h>
#include <stdio.h>
#include <errno.h>
#include <string.h>

int main(void) {
    size_t n = 6442450944ULL; /* 6 GiB, same size as the failing MT output buffer */
    void *p = mmap(NULL, n, PROT_READ | PROT_WRITE,
                   MAP_SHARED | MAP_ANONYMOUS | MAP_LOCKED, -1, 0);
    if (p == MAP_FAILED) {
        printf("MAP_LOCKED 6GB -> FAILED errno=%d=%s\n", errno, strerror(errno));
        return 1;
    }
    printf("MAP_LOCKED 6GB -> OK (%p)\n", p);
    return 0;
}
