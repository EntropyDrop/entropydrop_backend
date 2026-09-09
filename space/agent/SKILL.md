---
name: entropydrop-space
description: Use spaceAPI to query the key owner's position and create voxel entities or structures, edit owned entity code/defaults, and start or stop entities in EntropyDrop Space. Generate entityAPI component code when the requested entity needs programmable behavior.
---

# Space Agent Skill — spaceAPI and entityAPI

[spaceAPI](spaceAPI.md) · [entityAPI](entityAPI.md)

- **spaceAPI** is the HTTP interface used by agents and clients. Read the [spaceAPI guide](spaceAPI.md) before sending requests; it covers authorization, coordinates, freshness, creation, code/default edits, start/stop, quotas, and error handling.
- **entityAPI** is the runtime interface called by entity component code using `self` and `ctx`. Read the [entityAPI reference](entityAPI.md) when generating component code; submit that code through spaceAPI for the entity runtime to execute.

Use the user-provided backend origin and spaceAPI key. If the user has not provided a spaceAPI key yet, ask them for an existing key, or guide them to open `/space/apikeys` on the frontend to generate one. Send the key only to that backend in the Authorization header. Do not put it in a URL, generated code, or a public artifact. Model-provider API keys are separate from spaceAPI credentials.

For a build near the player, follow the spaceAPI guide to read the key owner's saved position first. Reject stale or unavailable positions unless the user explicitly chooses otherwise. Keep operation IDs and request bodies stable across retries. Before editing an entity, read its configuration, stop it, and use the current revision. All valid API keys, including existing keys, have full Space permissions without selecting scopes; entities have only running and stopped states. Use only documented capabilities and preserve the requested task and server.

Read [entity encoding and request examples](references/entity-create.md) for Protobuf submission. These documents are public, but world operations still require authorization.
