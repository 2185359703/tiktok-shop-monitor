import { build } from 'esbuild';
import { createHash } from 'node:crypto';
import { readFile, writeFile } from 'node:fs/promises';

const builds = await Promise.all([
  build({entryPoints: ['app/frontend/app.js'], bundle: true, minify: true, format: 'esm',
    target: 'es2022', outfile: 'app/frontend/app.bundle.js', metafile: true}),
  build({entryPoints: ['app/frontend/styles.entry.css'], bundle: true, minify: true,
    external: ['./fonts/*', './assets/*'], outfile: 'app/frontend/styles.bundle.css', metafile: true})
]);
const files = [...new Set(builds.flatMap(result => [...Object.keys(result.metafile.inputs), ...Object.keys(result.metafile.outputs)]))].sort();
const hashes = Object.fromEntries(await Promise.all(files.map(async file => [file, createHash('sha256').update(await readFile(file)).digest('hex')])));
await writeFile('app/frontend/build-manifest.json', JSON.stringify({esbuild: '0.25.12', sha256: hashes}, null, 2) + '\n');
