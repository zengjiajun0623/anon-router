"""confetti — post-quantum ZK payment channels (Spec-v2 reference implementation).

See ~/cleavelabs/zk-payments-confetti/Spec-v2.md for the protocol of record.
The R_pay proof uses a swappable Prover; the default ClearWitnessProver is
sound but not zero-knowledge (that property is gated on the STARK backend).
"""
from .channel import (Contract, PaymentMessage, Payer, Recipient, SignedState)
from .relation import (ClearWitnessProver, GenesisBranch, SignedBranch,
                       Statement, Witness, check_R_pay)

__all__ = [
    "Contract", "Payer", "Recipient", "PaymentMessage", "SignedState",
    "ClearWitnessProver", "Statement", "Witness", "check_R_pay",
    "GenesisBranch", "SignedBranch",
]
