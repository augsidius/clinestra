# How Clinestra works

Clinestra runs longitudinal care. An application or agent submits operations to it. Clinestra validates each operation against the applicable rules, stores the result, records who performed it and advances any care programme the patient is enrolled in. See [Longitudinal care](longitudinal_care.md).

## Terminology

| Term | Definition |
|---|---|
| **Harness** | The component that performs all reads and writes and enforces the rules. The HTTP API and the agent tools both call it. |
| **Actor** | The identity performing an operation: an id, a site, a role, and whether it is a person or an agent. Over HTTP, the actor is derived from the credential. |
| **Site** | The tenant boundary. Records in one site are not visible from another. |
| **Resource** | One of the 31 typed records, such as a patient, an observation or a careplan. |
| **Lifecycle** | The permitted states of a resource and the permitted transitions between them. |
| **Frozen state** | A state in which a record can no longer be edited, such as a finalized note page. |
| **Definition** | A versioned template that configures a resource, such as a case sheet layout or a protocol. A published definition cannot be changed. |
| **Pack** | A distributable set of definitions. |
| **Reaction** | A configured response to a record entering a given state. |
| **Proposal** | A pending change submitted by an agent or a reaction, which takes effect only if a clinician accepts it. |
| **Approval** | A request to enter a gated state, decided by someone other than the requester. |
| **Timeline** | The hash-chained, per-patient record of every write. |

## Processing of a write

1. The credential is resolved to an actor.
2. The site boundary and the actor's role are checked.
3. Consent requirements, the resource's lifecycle and its validation rules are checked.
4. The record is stored and the write is appended to the patient's timeline.
5. Any matching reactions run. Effects on clinical resources are stored as proposals, and other effects are applied directly.

A failed check stops the operation and returns an error. See [status codes](api_basics.md#status-codes).

## Clinical and administrative operations

An operation is *clinical* if it records or changes a clinical decision, for example recording a condition, activating a careplan or finalizing a note. Clinical operations require a clinician and cannot be performed directly by an agent or a reaction. Administrative operations, such as creating a task, have no such restriction.

## Agents

An agent is an actor whose type is `agent`. It may read the record of a patient with an active `ai` consent, draft note pages, documents and conversations, and submit proposals. Its tools are scoped to a context, such as one patient, and the context sets the patient identifier, so the model cannot supply a different one. See [AI harness](ai_harness.md).

## Further reading

- [Quickstart](quickstart.md)
- [How-to guides](guides.md)
- [Example flows](flows.md)
- [API conventions](api_basics.md)
- [Clinical use](clinical_overview.md)
