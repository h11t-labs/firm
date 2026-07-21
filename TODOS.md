# TODOS

Deferred work with full context, so the reasoning survives until someone picks it up.

## Two-key split for audit tamper-evidence (writer key vs seal key)

- **What:** Separate HMAC keys: the row-MAC key stays on every app instance, a distinct
  seal key lives only on the host(s) allowed to run the sealer
  (`FIRM_AUDIT_SEAL_KEY`).
- **Why:** With the single `FIRM_AUDIT_KEY` everywhere (v1 design), compromising any one
  app instance leaks the key that can also forge seals — and therefore rewrite history
  undetected. With a split, that attacker can forge individual rows at most; the seal
  chain stays out of reach.
- **Pros:** Smaller blast radius on instance compromise; standard defense-in-depth.
- **Cons:** Two secrets to manage; the sealer becomes a designated role instead of
  "any instance may run it" (weakens the zero-election story); verify needs both keys.
- **Context:** See `DESIGN-audit-tamper-evidence.md` §Key management. Starting point:
  second env var, sealer only starts where it is set, seal MACs record which key kind
  signed them via the existing `key_id` mechanism.
- **Depends on / blocked by:** tamper-evidence v1 shipped (this branch).
