Dead-channel detection retains solver and native connection handles for cleanup
instead of permitting another launch. Owned sessions use bounded native exit and
process-finish verification; failed or unverifiable cleanup preserves the handles
and blocks reconnect until cleanup succeeds. Attached sessions and explicit
cleanup opt-outs retain their ownership semantics.
