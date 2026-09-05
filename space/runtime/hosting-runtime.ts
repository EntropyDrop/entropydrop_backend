import { createInterface } from 'node:readline';
import { HostedSimulation } from './HostedSimulation.ts';
import { preloadQuickJSScriptRuntime } from '@entropydrop/space-engine';

// Private, local pipe protocol. Guest JavaScript only runs inside QuickJS/WASM.
// Keep stdout exclusively for framed responses.
console.log = (...args) => console.error(...args);
let simulation: HostedSimulation | null = null;
let worldId = '';
for await (const line of createInterface({ input: process.stdin, crlfDelay: Infinity })) {
  try {
    if (line.length > 32 * 1024 * 1024) throw new Error('hosting_input_limit');
    const input = JSON.parse(line);
    if (input.probe === true) {
      await preloadQuickJSScriptRuntime();
      process.stdout.write('{"ready":true}\n');
      continue;
    }
    if (!simulation || worldId !== input.world_id) {
      worldId = input.world_id;
      simulation = new HostedSimulation(input.seed);
    }
    process.stdout.write(JSON.stringify(await simulation.step(input)) + '\n');
  } catch (error) {
    process.stdout.write(JSON.stringify({ error: String(error).slice(0, 500) }) + '\n');
    simulation = null;
  }
}
