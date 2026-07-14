#!/usr/bin/env bash
# reg.sh — read guests.registry. Source it, then:
#   reg_list                # print the guests + their capabilities
#   reg_lookup <name>       # 0 if found (matches the `name` OR `run_guest` column), else 1;
#                           # on success sets:
#     REG_INFRA REG_RUNG REG_ETHVAR REG_VENDOR REG_ELF REG_WIT REG_EXEC REG_DESC
#
# Single parser for every root tool (gen-witness · gen-elf · execute) — the registry data
# in guests.registry is the source of truth; this is just the reader.

REG_FILE="${REG_FILE:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/guests.registry}"

reg_list() {
  awk -F'|' 'function t(s){gsub(/^[ \t]+|[ \t]+$/,"",s);return s}
    !/^#/ && NF>=9 { printf "  %-11s elf:%-11s witness:%-11s exec:%-6s %s\n",
      t($1), t($6), t($7), t($8), t($9) }' "$REG_FILE"
}

reg_lookup() {
  local g="$1" row
  row="$(awk -F'|' -v g="$g" '
    function t(s){gsub(/^[ \t]+|[ \t]+$/,"",s);return s}
    !/^#/ && NF>=9 { if (t($1)==g || t($3)==g) {
        printf "%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s",
          t($2),t($3),t($4),t($5),t($6),t($7),t($8),t($9); exit } }' "$REG_FILE")"
  [ -n "$row" ] || return 1
  IFS=$'\t' read -r REG_INFRA REG_RUNG REG_ETHVAR REG_VENDOR REG_ELF REG_WIT REG_EXEC REG_DESC <<<"$row"
}
