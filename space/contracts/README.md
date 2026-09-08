# Space protocol contracts

The canonical inventory resource schema now lives in
`entropydrop_space_engine/proto/inventory.proto`. This directory retains the generated
Python binding so the Python API can run without a Node or engine checkout.

From the backend root, with protoc 33.2 (Python gencode 6.33.2):

```sh
protoc --proto_path=space/contracts=../entropydrop_space_engine/proto --python_out=. space/contracts/inventory.proto
```

The virtual path keeps the existing `space.contracts.inventory_pb2` module and Protobuf
descriptor identity. Regenerate and check in the binding whenever the shared schema changes.
`protocol.proto` and `schema.sql` remain the backend's multiplayer storage contracts.

The current construction grid has 8 divisions per metre. Inventory schema v6 is
required. Standalone migration `space_0003` targets fresh Space content; deployments with
old 5-grid terrain, entities, market objects and derived surface data must reset
that Space content before switching versions. No legacy conversion or automatic
database deletion is performed by this change.
