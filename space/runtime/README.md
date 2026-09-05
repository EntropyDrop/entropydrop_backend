# Space hosting runtime

Backend-owned Node simulation process. `hosting-runtime.ts` handles the private stdin/stdout
protocol, and `HostedSimulation.ts` applies the hosting limits and runs the engine.
Python owns authentication, persistence, worker leases, and billing.

Requires Node 24+ and the sibling `entropydrop_space_engine` checkout with its own
dependencies installed (`npm ci` in that repository). From this directory:

```sh
npm ci
npm run build
```

`@entropydrop/space-engine` is a local `file:` development dependency on the engine
repository. esbuild includes it in `dist/hosting-runtime.mjs`;
QuickJS packages stay external so their WASM loader can find its data file. To run on
another machine, ship `dist/`, `package.json`, and `package-lock.json`, install production
dependencies with `npm ci --omit=dev`, and point Python's `SPACE_HOSTING_RUNTIME_PATH`
at the built entry if it is outside this directory. No frontend or engine source is needed there.

From the backend root, `DATABASE_URL=sqlite:///:memory: python -m space.hosting_smoke`
checks execution and restart recovery without enabling hosting. Hosting API and worker
execution remain disabled unless `SPACE_HOSTING_ENABLED=true` is explicitly configured.
See [the hosting guide](../../docs/space-entity-hosting.md) for deployment details.
