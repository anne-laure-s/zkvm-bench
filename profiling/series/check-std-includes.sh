#!/usr/bin/env bash
# Proxy bon marché pour la classe d'erreurs misc-include-cleaner la plus fréquente :
# un symbole std:: utilisé sans que son en-tête soit inclus directement.
# Ce n'est PAS le check ; c'est ce qui rattrape 90 % des cas avant de pousser.
set -uo pipefail
map='span:span vector:vector string_view:string_view optional:optional variant:variant
array:array tuple:tuple deque:deque set:set map:map unordered_map:unordered_map
unordered_set:unordered_set bitset:bitset atomic:atomic mutex:mutex thread:thread
function:functional unique_ptr:memory shared_ptr:memory make_unique:memory
make_shared:memory move:utility forward:utility pair:utility exchange:utility
size_t:cstddef byte:cstddef sort:algorithm find_if:algorithm copy:algorithm'
rc=0
for f in "$@"; do
  [ -f "$f" ] || continue
  case "$f" in *.cpp|*.c) ;; *) continue ;; esac
  for e in $map; do
    sym=${e%%:*}; hdr=${e##*:}
    grep -qE "\bstd::${sym}\b" "$f" || continue
    grep -qE "^#include <${hdr}>" "$f" && continue
    echo "$f: utilise std::${sym} sans #include <${hdr}>"
    rc=1
  done
done
exit $rc
