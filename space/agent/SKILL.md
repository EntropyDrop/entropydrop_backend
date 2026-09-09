---
name: entropydrop-space
description: Use spaceAPI to query the key owner's position and create voxel entities or structures, edit owned entity code/defaults, and start or stop entities in EntropyDrop Space. Generate entityAPI component code when the requested entity needs programmable behavior.
---

# Space Agent Skill — spaceAPI and entityAPI

[spaceAPI](spaceAPI.md) · [entityAPI](entityAPI.md)

- **spaceAPI** is the HTTP interface used by agents and clients. Read the [spaceAPI guide](spaceAPI.md) before sending requests; it covers authorization, coordinates, freshness, creation, code/default edits, start/stop, quotas, and error handling.
- **entityAPI** is the runtime interface called by entity component code using `self` and `ctx`. Read the [entityAPI reference](entityAPI.md) when generating component code; submit that code through spaceAPI for the entity runtime to execute.

## Authorization & API Keys
All world operations and position queries require a user-provided spaceAPI Key:
- **Obtaining a Key**: If the user has not provided a spaceAPI key yet, check whether one was given in the conversation. If not, ask the user for an existing key, or guide them to open `/space/apikeys` on the frontend (e.g. `{origin}/space/apikeys`) to create one.
- **Header**: Send the key only in the `Authorization: Bearer <key>` HTTP header to the designated backend. Never put the key in URLs, generated code, or public artifacts. Model-provider API keys are separate from spaceAPI credentials.
- **Permissions**: All valid API keys (including existing keys) have full Space permissions without selecting scopes.

## Agent Workflow
1. **API Key & Position**: Ensure you have the user's spaceAPI key. Then query the player's saved position via `GET /space/api/v2/players/me/position`. If the position is stale (> 30s) or unavailable, ask the user to enter the online Space world to refresh their coordinates.
2. **Requirements & Planning**: Clarify what the user wants to build. Plan voxel geometry, dimensions, colors, and components.
3. **Programmable Scripts**: If the entity has programmable parts (thrusters, hinges, spinners, sensors), write `entityAPI` scripts using `self` and `ctx`.
4. **Execution**: Submit the construction via spaceAPI. Keep operation IDs and request bodies stable across retries.

When editing an existing entity, read its configuration, stop it, and submit updates with the current revision. Entities have running and stopped states. Use only documented capabilities and preserve the requested task and server.

Read [entity encoding and request examples](references/entity-create.md) for Protobuf submission. These documents are public, but world operations still require authorization.
