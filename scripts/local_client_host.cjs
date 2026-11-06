// Local static Web client; API and SSE are streamed to the SSH-forwarded server.
const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');
const root = __dirname;
const config = JSON.parse(fs.readFileSync(path.join(root, 'install.json'), 'utf8'));
const dist = path.resolve(root, 'client', 'dist');
const mime = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8', '.json': 'application/json', '.svg': 'image/svg+xml', '.png': 'image/png', '.ico': 'image/x-icon', '.woff2': 'font/woff2' };
const server = http.createServer((req, res) => {
  if (req.url === '/_client/health') {
    res.writeHead(200, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' });
    return res.end(JSON.stringify({ product: 'zhixing-local-client', version: config.version, assets: 'local', server: 'tencent' }));
  }
  if (req.url.startsWith('/api/')) {
    const headers = { ...req.headers };
    delete headers.connection;
    const upstream = http.request({ hostname: '127.0.0.1', port: config.tunnel_port, method: req.method, path: req.url, headers }, response => {
      const outgoing = { ...response.headers };
      delete outgoing.connection;
      res.writeHead(response.statusCode, outgoing);
      response.pipe(res);
      response.on('error', () => res.destroy());
    });
    upstream.on('error', () => {
      if (!res.headersSent) res.writeHead(502, { 'Content-Type': 'application/json' });
      if (!res.destroyed) res.end(JSON.stringify({ error: { code: 'REMOTE_UNAVAILABLE', message: '云端连接暂不可用，请重新打开知行工作台。' } }));
    });
    req.on('aborted', () => upstream.destroy());
    res.on('close', () => upstream.destroy());
    return req.pipe(upstream);
  }
  if (!['GET', 'HEAD'].includes(req.method)) { res.writeHead(405); return res.end(); }
  let pathname;
  try { pathname = decodeURIComponent(new URL(req.url, 'http://localhost').pathname); }
  catch { res.writeHead(400); return res.end(); }
  let file = path.resolve(dist, '.' + pathname);
  if (!file.startsWith(dist + path.sep) && file !== dist) { res.writeHead(403); return res.end(); }
  if (!fs.existsSync(file) || !fs.statSync(file).isFile()) {
    if (pathname.startsWith('/assets/') || path.extname(pathname)) { res.writeHead(404); return res.end(); }
    file = path.join(dist, 'index.html');
  }
  res.writeHead(200, { 'Content-Type': mime[path.extname(file)] || 'application/octet-stream', 'Cache-Control': pathname.startsWith('/assets/') ? 'public, max-age=31536000, immutable' : 'no-cache', 'X-Content-Type-Options': 'nosniff', 'Content-Length': fs.statSync(file).size });
  if (req.method === 'HEAD') return res.end();
  fs.createReadStream(file).on('error', () => res.destroy()).pipe(res);
});
server.requestTimeout = 0;
server.listen(config.client_port, '127.0.0.1', () => console.log('Local client ready'));
server.on('error', error => { console.error(error.code || 'Local client error'); process.exit(1); });
process.on('SIGTERM', () => server.close(() => process.exit(0)));
