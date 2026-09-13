# entityAPI V2

<!-- GENERATED from src/contraption/ScriptApiContract.ts. Do not edit by hand. -->

[spaceAPI](spaceAPI.md) · [entityAPI](entityAPI.md)

entityAPI is called by entity component code through `self` and `ctx` inside the runtime. spaceAPI is the authenticated HTTP API used by agents and clients to query or change Space. Agents may generate entityAPI code; the entity runtime executes it.

Canonical contract for component scripts running with `(self, ctx)` in the Space voxel-physics world.
## Defaults and coordinate conventions

- Coordinates are right-handed and Y-up: +X right, +Y up, -Z forward. Euler angles use YXZ order; quaternions are `[x,y,z,w]`.
- A component pivot starts at its own block AABB centroid and never moves automatically after block edits. Use `getBounds()` then `setPivot(bounds.center)` to recenter a kinematic body without moving its blocks.
- Component IDs are unique across the entire entity; no string is reserved. The root is identified structurally by `parentId:null`, and a child's local position is its pivot offset in the parent pivot frame.
- Entities have only running and stopped states. Start enables entity physics and all component scripts. Stop disables physics and scripts, clears state/time/tick/motion, resets child transforms, and restores persisted BodyConfig defaults. Individual component code switches do not create a third entity state.
- BodyConfig defaults are type, mass, restitution, friction, gravity, and collision. Script setters are runtime-only; serialization always writes defaults.
- Collision defaults to enabled. A disabled component remains rendered/editable but has no terrain, player, entity, or raycast shapes.

## World topology

- The world is a torus: X is `[0,16384)` and Z is `[0,2048)` for world voxel operations and raycasts. Y does not wrap; voxel edits require Y in `[0,256)`.
- `ctx.position` is continuous and does not wrap. Follow/orbit/seek logic must use shortest wrapped X/Z deltas.
- An entity whose root falls below `y = -30` is removed with its scripts and state.

**Shortest wrapped delta**

```js
function wrappedDelta(from, to, size) {
  return ((to - from) % size + size + size / 2) % size - size / 2;
}
const dx = wrappedDelta(ctx.position[0], target[0], 16384);
const dz = wrappedDelta(ctx.position[2], target[2], 2048);
```

## Execution model

- Every component script receives `(self, ctx)` once per fixed 20 Hz entity tick. `self` is the target component; root body fields in `ctx` always describe the root entity.
- The root script runs before child scripts. All components share one frozen frame-start `ctx` snapshot; admitted commands commit after the synchronous QuickJS tick.
- Each component owns `self.state`. Completed state survives chunk streaming and disabling component code; Stop clears it.
- Queued mutation success means command-buffer admission (`reason:'queued'`), not final commit. Successful admission includes `commandId`; the main thread revalidates bounds, occupancy, and permissions and publishes the final result through `ctx.commands` on the next submitted frame.
- Limits: 4 MiB runtime memory, 512 KiB stack, 64 components per entity, 256 commands, 256 world voxel reads, and 64 raycasts per tick, 5 ms per component invocation, 25 ms aggregate entity time, and 64 VM interrupt checkpoints.
- A component exception disables that component. Aggregate time/checkpoint failure disables every component script and discards commands from the interrupted tick.
- Entities only exist and run while their wrapped root chunk is active; streaming serializes identity, hierarchy, physics, scripts, defaults, and completed state.

## ctx — read-only frame snapshot

| API | Type | Description |
| --- | --- | --- |
| `ctx.apiVersion` | number | Current entityAPI version: `2`. |
| `ctx.entityId` | string | Stable random ID of the current entity. |
| `ctx.root` | ComponentAPI | Root component and entry point for recursive tree traversal. |
| `ctx.time` | number | Seconds with at least one component script enabled; disabled code does not advance it and Stop resets it. |
| `ctx.deltaTime` | number | Fixed entity simulation step: always `0.05` seconds (20 Hz); scripts cannot change it. |
| `ctx.tick` | number | Executed script-frame count; disabled code does not advance it and Stop resets it. |
| `ctx.position` | [x,y,z] | Root entity world position. It is continuous and does not wrap at the torus seam. |
| `ctx.velocity` | [x,y,z] | Root world-space velocity in m/s. |
| `ctx.rotation` | [x,y,z] | Root Euler angles in radians using YXZ order. |
| `ctx.angularVelocity` | [x,y,z] | Root angular velocity in rad/s. |
| `ctx.groundDistance` | number | Distance in metres to the ground below. |
| `ctx.isOnGround` | boolean | Whether the root dynamic body was supported during the latest completed physics frame. |
| `ctx.mass` | number | Root entity mass in kg. |
| `ctx.bodyType` | string | Root body type: `'kinematic'` or `'dynamic'`. |
| `ctx.gravity` | [x,y,z] | Current gravity vector; default `[0,-18,0]`. |
| `ctx.limits` | object | `{maxForce,maxTorque}` for the legacy root-body force surface only. |
| `ctx.input` | object | Keyboard edge/held-state API described below. |
| `ctx.blocks` | object | Block-edit snapshot: `pressed(type?)` and `event()`; types are `'place'\|'remove'\|'color'\|'subdivide'`. |
| `ctx.players` | array | Frozen player observations. `position` remains the eye-position compatibility alias; records also expose `eyePosition`, nullable `feetPosition`/`velocity`/pose and movement flags, riding IDs, `isLocal`, and fixed 50 kg mass. |
| `ctx.driver` | object|null | Current local driver for this entity as `{playerId,componentId,seatIndex}`, or `null` when it is not mounted. |
| `ctx.contacts` | array | Up to 32 frozen contacts observed since the previous submitted script frame. Kinds are `terrain\|entity\|player`; records include component IDs, point, normal, relative velocity, penetration, and impulse when available. Resting support contacts retained during physics sleep have `sleeping: true` and zero impulse/relative velocity. |
| `ctx.world` | object | World query and mutation API described below. |
| `ctx.selection` | object | Shared engine selection command API described below. |
| `ctx.commands` | object | Final main-thread command results from the previous submitted frame: `get(commandId)` and `all()`. |
| `ctx.log(msg)` | function | Append one line to the component console. |

## Component API (self)

Every root and child receives the same top-level API. Namespaces target the current component unless explicitly described as legacy root-body methods.

### Universal component surface

| API | Description |
| --- | --- |
| `self.apiVersion` | Current component API version: `2`. |
| `self.id / self.parentId` | Component ID and direct parent ID; the root has an ordinary ID and `parentId:null`. |
| `self.state` | Mutable persistent state scoped to this component and retained across completed ticks and streaming. |
| `self.child(id)` | Look up a direct child by its ordinary ID; returns `null` when missing. |
| `self.children()` | Return a frozen array of direct children. Recurse from `ctx.root` to traverse the tree. |
| `self.applyThrust([x,y,z])` | Apply root-local force at this component. A child mounting offset produces torque; dynamic root only and subject to `ctx.limits`. |
| `self.applyLocalThrust([x,y,z])` | Apply component-local force at this component. Installed anchor orientation controls its direction and an offset produces torque. |
| `self.applyForce([x,y,z])` | Apply world-space force to the root center of mass; no effect on a kinematic root. |
| `self.applyLocalForce([x,y,z])` | Apply root/body-local force. A child's direction is interpreted in that component's local frame. |
| `self.applyForceAt(force, localPoint)` | Apply world-space force at a component-local offset, producing translation and torque. |
| `self.applyTorque([x,y,z])` | Apply world-space torque to the root body. |
| `self.getWorldPosition()` | Return this component world position as `[x,y,z]`. |
| `self.getWorldRotation()` | Return this component world quaternion `[x,y,z,w]`, including ancestor rotations. |
| `self.localToWorldDirection(dir)` | Convert a component-local direction to world space. |
| `self.getPivot()` | Return the rotation pivot in entity-local coordinates. |
| `self.getBounds()` | Return entity-local block bounds `{min,max,size,center}`, or `null` when empty. |
| `self.setSeats(seats)` | Replace this component's pivot-relative driver seats. Each entry is `[x,y,z]` or `{position,rotation?,fixedOrientation?}`; `rotation` is a `[x,y,z,w]` rider orientation in the pivot frame (default identity, facing the component's -Z forward) and `fixedOrientation:true` makes a mounted rider's yaw follow the seat's solved world orientation so the view swings with the vehicle. Invalid positions or degenerate quaternions drop that seat. An entity is mountable when any component has a seat. |
| `self.getSeats()` | Return this component's pivot-relative driver seats as `{position,rotation,fixedOrientation}` records. |
| `self.voxels.set(position, options?)` | Queue one pivot-relative standard voxel placement; returns `{ok,placed,reason}`. |
| `self.voxels.clear(position)` | Queue removal of one standard voxel; returns `{ok,removed,reason}`. |
| `self.voxels.paint(position, options?)` | Queue repainting one standard voxel; returns `{ok,painted,reason}`. |
| `self.voxels.clearCell(position)` | Queue removal of all standard and micro voxels in one 1 m component cell. |
| `self.voxels.subdivide(position, clearOffset?)` | Queue conversion to 512 micro voxels, optionally removing one offset atomically. |
| `self.microVoxels.set(cell, offset, options?)` | Queue a 0.125 m voxel; each offset coordinate is an integer from 0 through 7. |
| `self.microVoxels.clear(cell, offset)` | Queue removal of one exact 0.125 m component voxel. |
| `self.microVoxels.paint(cell, offset, options?)` | Queue repainting one exact 0.125 m component voxel. |

> Component voxel cells are measured from the current pivot, not the entity corner. Fractional cell coordinates floor after applying the pivot.

> All voxel changes are queued and action-specific. Check `result.ok` and `result.reason`; entity bounds are capped at 64×64×64.

> Removing an entity's final voxel deletes the entity, scripts, and state.
### Kinematics

Only kinematic bodies accept direct pose commands; dynamic bodies are solver-driven.

| API | Description |
| --- | --- |
| `self.setLocalPosition([x,y,z])` | Set a kinematic child relative to its parent, or a kinematic root in world space. |
| `self.setLocalEuler([x,y,z])` | Set kinematic orientation in radians using YXZ Euler order. |
| `self.setLocalRotation([x,y,z,w])` | Set kinematic orientation as a quaternion. |
| `self.setLocalSpin([ax,ay,az], rpm)` | Command continuous local-axis spin; call each entity tick to sustain it. |
| `self.getLocalPosition()` | Return current local position as `[x,y,z]`; root returns `[0,0,0]`. |
| `self.getLocalRotation()` | Return current local quaternion `[x,y,z,w]`. |
| `self.setPivot([x,y,z])` | Set a kinematic pivot in entity-local coordinates while preserving block world positions. Dynamic bodies use their physical center of mass. |
### Rigid body, constraints, and Stop

| API | Description |
| --- | --- |
| `self.body.getType() / self.body.setType(type)` | Read or set `'kinematic'\|'dynamic'`; setter returns `{ok,type,reason}`. Entity bodies have no static type. |
| `self.body.getMass() / self.body.setMass(kg)` | Read or set runtime mass; setter returns `{ok,mass,reason}`. Automatic mass is owned block count × 10 kg; minimum is 0.1 kg and invalid input returns `invalid_mass`. |
| `self.body.getMaterial() / self.body.setMaterial(options)` | Read or set `{restitution,friction}`; setter returns `{ok,material,reason}`. Both coefficients clamp to `[0,1]` and default to 0.1/0.7. |
| `self.body.getGravityEnabled() / self.body.setGravityEnabled(bool)` | Read or set runtime gravity; setter returns `{ok,enabled,reason}`. Kinematic bodies retain the flag but are not gravity-driven. |
| `self.body.getCollisionEnabled() / self.body.setCollisionEnabled(bool)` | Read or set collision participation for terrain, player, entity, and raycast shapes; setter returns `{ok,enabled,reason}`. |
| `self.body.getVelocity() / self.body.getAngularVelocity()` | Read this component body's world-space velocities. |
| `self.body.applyForce(force)` | Apply world force to this dynamic component body; returns boolean. |
| `self.body.applyLocalForce(force)` | Apply body-local force to this dynamic component body; returns boolean. |
| `self.body.applyTorque(torque)` | Apply world torque to this dynamic component body; returns boolean. |
| `self.constraints.all()` | Return a frozen snapshot of constraints connected to this component. |
| `self.constraints.create({id?,type,bodyA?,anchorA?,anchorB?,axisA?,axisB?,limits?,stiffness?,collideConnected?})` | Queue a `point`, `hinge`, or `weld`; `bodyA:null` denotes the external world and an omitted `bodyA` uses the structural parent (or external world for the root). Immediate Worker success is provisional `{ok:true,id:null,reason:'queued'}`. Supply an explicit ID for later lookup. Stiffness defaults to 0.9, `collideConnected` to false, omitted anchors use pivots, and hinge limits are radians. |
| `self.constraints.remove(id)` | Queue removal of one constraint; returns boolean. |
| `self.stop()` | Root-only global Stop: disable entity physics and scripts, clear state/time/tick/motion, reset child poses, and restore persisted BodyConfig defaults. Collision and selection shapes remain active. Child code must call `ctx.root.stop()`. |

> `self.body` setters alter runtime values only. They persist until global Stop, which disables dynamics while retaining static collision/query shapes and restores persisted defaults.

> `self.body.apply*` targets the current component body and bypasses the legacy `ctx.limits`/HUD budget, but rejects non-finite values and components above `1e12`.

> Constraint creation is queued. Supply an explicit ID when later script logic requires a stable name; hinge limits are radians.

## ctx.world

| API | Description |
| --- | --- |
| `ctx.world.apiVersion` | Current world API version: `2`. |
| `ctx.world.voxels.get(position)` | Read a real standard world voxel as `{block,color}` plus the current tick's admitted-write overlay; maximum 256 combined standard/micro host reads per entity tick. |
| `ctx.world.voxels.set(position, options?)` | Queue a standard placement; admitted result is provisional `{ok:true,placed:1,reason:'queued'}`. |
| `ctx.world.voxels.clear(position)` | Queue removal of one standard voxel without deleting micro voxels in its cell. |
| `ctx.world.voxels.paint(position, options?)` | Queue repainting one existing standard voxel. |
| `ctx.world.voxels.clearCell(position)` | Queue removal of all standard and micro voxels in one world cell. |
| `ctx.world.voxels.subdivide(position, clearOffset?)` | Queue conversion of one standard voxel to 512 micro voxels. |
| `ctx.world.microVoxels.get(cell, offset)` | Read one real 0.125 m world voxel as `{block,color}` plus the current tick overlay; offset coordinates are integers from 0 through 7. |
| `ctx.world.microVoxels.set(cell, offset, options?)` | Queue one 0.125 m world voxel placement. |
| `ctx.world.microVoxels.clear(cell, offset)` | Queue removal of one exact 0.125 m world voxel. |
| `ctx.world.microVoxels.paint(cell, offset, options?)` | Queue repainting one existing micro world voxel. |
| `ctx.world.entities(origin, radius=16)` | Filter the prefetched 64 m nearby-entity snapshot using shortest wrapped X/Z distance. Descriptors include pose, velocities, mass, bounds, collision/ground state, physics enabled state, script status, and component count. |
| `ctx.world.entities.get(id, chunkId?)` | Look up an entity in the frozen nearby snapshot. |
| `ctx.world.entities.list(chunkId) / inChunk(chunkId)` | Filter nearby entities by wrapped chunk ID `"cx,cz"`. |
| `ctx.world.raycast(origin, direction, maxDistance=24)` | Compatibility form: bounded synchronous standard-world-voxel raycast. |
| `ctx.world.raycast(origin, direction, {maxDistance=24,include='world',voxelKinds=['standard'],space='world'})` | Full existing engine raycast over standard/micro world voxels and/or entities. Returns normalized kind, voxelKind, IDs, block/color, normal, position, and distance; maximum 64 calls per entity tick. |

> World writes never overwrite occupied cells or implicitly convert between standard and micro voxels. X/Z wrap automatically.

## ctx.selection

| API | Description |
| --- | --- |
| `ctx.selection.get()` | Read the current frozen shared selection snapshot. |
| `ctx.selection.clear()` | Queue clearing the shared selection; returns `{ok,cleared,reason}`. |
| `ctx.selection.cornerA(point) / cornerB(point)` | Set progressive world-box corners; accepts `{micro:true}` and returns `{ok,selected,reason}`. |
| `ctx.selection.box(a, b)` | Set an atomic world box; accepts `{micro:true}`. |
| `ctx.selection.cells(list) / toggle(cell)` | Replace or toggle sparse cells; micro mode uses 0.125 m cells. |
| `ctx.selection.entity(entityId, nodeId?)` | Select a component subtree. Internal component selection requires a stopped entity. |
| `ctx.selection.entityBox(entityId, nodeId, a, b, space?)` | Select directly owned voxels intersecting a node-local or world-space box; requires stopped. |
| `ctx.selection.delete()` | Delete the shared selection; internal entity edits require stopped. Returns removal counts and IDs. |
| `ctx.selection.assemble(mode='programmable', options={})` | Assemble world voxels in `auto\|free_physics\|projectile\|programmable` mode; options are `{bodyType,restitution,friction,useGravity,mass}` and result is `{ok,assembled,entityId,runtimeId,reason}`. Invalid mode fails before changing selection or world. |
| `ctx.selection.createChild(id?)` | Create a child from selected entity blocks; requires stopped and returns `{ok,childId,reason}`. |

> Selections are capped at a 64×64×64 AABB. Boxes can clamp; sparse operations that exceed the cap fail with `bounds_exceeded`.

> Mutations update an optimistic in-tick snapshot, but the main thread can still reject internal entity edits with `entity_not_stopped`.

> Assembly defaults: dynamic body, restitution 0.1, friction 0.7, gravity enabled for dynamic bodies, and mass equal to owned block count × 10 kg with a 0.1 kg minimum.

> Gate destructive selection commands with `self.state` or an input edge so they run once.

## ctx.input and ctx.blocks

- `ctx.input.down(code)`, `pressed(code)`, and `released(code)` expose held, leading-edge, and trailing-edge keyboard state.
- Only the mounted entity receives player input. Generic `Shift`, `Control`, and `Alt` match either side.
- Reserved keys never reach scripts: Escape, Backspace, Delete, F3, F5, C, E, F, G, R, V, and digits 0–9.
- `ctx.blocks.pressed(type?)` is a one-tick edit edge for `place|remove|color|subdivide`; `event()` returns type/node, source/player, affected cell(s), voxel metadata, truncation, and final blockCount when available.

## Legacy root force budget

- `self.applyForce`, `applyLocalForce`, `applyThrust`, `applyForceAt`, and `applyTorque` target the root and share `maxForce = max(80, mass×65)` and `maxTorque = max(40, maxForce×max(0.75,boundingRadius))`.
- The engine clamps that legacy surface and displays its utilization in the Root Power Budget HUD.
- `self.body.apply*` is independent of that gameplay budget. Both force surfaces reject non-finite components and values above the separate `1e12` safety ceiling.

## Editor tips

- Ctrl/Cmd+Enter saves code and returns to the game; Escape closes the panel.
- Mount with V before writing a driving script; only mounted entities receive keyboard input.
- Use `self.state` for target values, phases, counters, and timers.
- Use `ctx.deltaTime` only when explicitly integrating a rate. Forces and torques are already commands in N/N·m.
