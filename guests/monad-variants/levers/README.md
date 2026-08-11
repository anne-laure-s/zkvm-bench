# monad-levers

The Monad guest built from branch `al/zkvm-levers` (11 commits on `ed16787ae`), for the
`levers-*` axes of `compare.py`. **Not what ships** — `guests/monad-{zisk,sp1}` are.

    monad-levers-zisk.elf   built on the devcore box, .text 1,410,308
    monad-levers-sp1.elf    built on the devcore box, .text 1,623,112

ZisK: 504 blocks measured through `guests/monad/ev.sh`, 504/504 post-state roots PASS.
SP1: cycles only — the runner does not expose the committed public values, so that side carries no
root verdict.

Neither carries the two submodule patches (`third_party/patches/`), worth a further 0.40 point.
