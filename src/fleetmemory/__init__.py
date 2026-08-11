"""FleetMemory — production-grade shared memory for an agent fleet.

Long-term layer: CockroachDB (bi-temporal facts + C-SPANN vectors),
guarded by a write-gate with a full decision journal.
Short-term layer: AWS AgentCore Memory (LangGraph checkpointer).
"""

__version__ = "0.1.0"
