import Lake
open Lake DSL

/-
Confetti on-chain contract model (anon-router M4b).

Self-contained: no mathlib / VCV-io dependency — the model and its safety
proofs need only core Lean (`omega`, `simp`). Toolchain pinned to the same
release as ../../zk-payments-confetti/lean so this library can live alongside
the Zkpc development.
-/
package ConfettiContract where
  leanOptions := #[
    ⟨`pp.unicode.fun, true⟩,
    ⟨`autoImplicit, false⟩,
    ⟨`relaxedAutoImplicit, false⟩
  ]

@[default_target] lean_lib ConfettiContract
