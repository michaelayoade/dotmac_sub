// Local visual review only. No live form endpoint or advertising tracker is served.
const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');
const root = path.resolve(__dirname, '../../docs/runbooks/website/fiber');
http.createServer((request, response) => {
  const url = new URL(request.url, 'http://127.0.0.1:8768');
  if (url.pathname === '/coverage/') {
    let html = fs.readFileSync(path.join(root, 'coverage-handoff-candidate.html'), 'utf8');
    html = html.replace(/<script\b[^>]*>[\s\S]*?<\/script>/gi, script => /fbq|gtag|googletagmanager|dotmac-whatsapp-tracking/.test(script) ? '' : script);
    response.setHeader('Content-Type', 'text/html; charset=utf-8');
    response.end(html);
  } else if (url.pathname === '/wp-content/mu-plugins/coverage-handoff.js') {
    response.setHeader('Content-Type', 'application/javascript');
    response.end(fs.readFileSync(path.join(root, 'coverage-handoff.js')));
  } else if (url.pathname === '/wp-json/dotmac-fiber/v1/attribution') {
    response.setHeader('Content-Type', 'application/json');
    response.end('{}');
  } else {
    response.writeHead(404); response.end('Preview only: live submissions are disabled.');
  }
}).listen(8768, '127.0.0.1', () => console.log('Local preview: http://127.0.0.1:8768/coverage/ (tracking and submissions disabled)'));
