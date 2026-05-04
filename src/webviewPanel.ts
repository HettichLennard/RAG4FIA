import * as vscode from 'vscode';
import * as path from 'path';

export function createWebviewPanel(
  context: vscode.ExtensionContext,
  title: string
): vscode.WebviewPanel {
  const panel = vscode.window.createWebviewPanel(
    'vdgGenerator',
    `VDG: ${title}`,
    vscode.ViewColumn.Beside,
    {
      enableScripts: true,
      localResourceRoots: [
        vscode.Uri.file(path.join(context.extensionPath, 'media')),
      ],
    }
  );

  const mediaPath = vscode.Uri.file(path.join(context.extensionPath, 'media'));
  const cssUri = panel.webview.asWebviewUri(
    vscode.Uri.joinPath(mediaPath, 'webview.css')
  );
  const jsUri = panel.webview.asWebviewUri(
    vscode.Uri.joinPath(mediaPath, 'webview.js')
  );
  const d3Uri = panel.webview.asWebviewUri(
    vscode.Uri.joinPath(mediaPath, 'd3.v7.min.js')
  );

  panel.webview.html = getHtml(cssUri, jsUri, d3Uri);

  return panel;
}

function getHtml(cssUri: vscode.Uri, jsUri: vscode.Uri, d3Uri: vscode.Uri): string {
  return `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <link rel="stylesheet" href="${cssUri}" />
  <title>VDG Generator</title>
</head>
<body>
  <div id="config-screen">
    <div class="config-box">
      <h2>Chunk Generation</h2>
      <div class="config-field">
        <label for="max-node-size">Max chunk size (lines of code)</label>
        <input id="max-node-size" type="number" min="0" value="100" />
        <span class="config-hint">Nodes with a range &le; this value are collapsed into leaves. Set to 0 to disable pruning.</span>
      </div>
      <div class="config-field config-checkbox">
        <label>
          <input id="preserve-scoping" type="checkbox" checked />
          Preserve scoping structure
        </label>
        <span class="config-hint">Keep scope-defining instructions (if, for, while, functions, classes, ...) as separate nodes during pruning.</span>
      </div>
      <button id="start-analysis">Generate Chunks</button>
    </div>
  </div>
  <div id="tree-container" style="display:none;">
    <svg id="tree-svg"></svg>
    <div id="stats-overlay"></div>
    <div id="zoom-controls">
      <button id="zoom-in" title="Zoom in">+</button>
      <button id="zoom-out" title="Zoom out">&minus;</button>
      <button id="export-png" title="Export PNG"><svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor"><path d="M14 1H2a1 1 0 0 0-1 1v12a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1V2a1 1 0 0 0-1-1zm-1 12H3V3h10v10z"/><circle cx="5.5" cy="5.5" r="1.25"/><path d="M3 13l3.5-4.5 2 2.5L11 7.5l2 5.5H3z"/></svg></button>
    </div>
    <div id="context-menu" style="display:none;"></div>
    <div id="loading-overlay">
      <div class="spinner"></div>
      <p>Generating chunks...</p>
    </div>
  </div>
  <div id="action-bar">
    <button id="btn-regenerate" title="Re-parse source files and regenerate chunks">Regenerate Chunks</button>
    <button id="btn-generate-deps" title="Generate chunks first" disabled>Generate Summaries</button>
    <span class="action-separator"></span>
    <label class="action-label" for="similarity-threshold">Similarity top %</label>
    <input id="similarity-threshold" type="number" min="0.1" max="100" step="0.5" value="5" class="action-input" />
    <button id="btn-generate-edges" title="Generate summaries first" disabled>Generate Edges</button>
    <span class="action-separator"></span>
    <button id="btn-neo4j" title="All edges required" disabled>Start Neo4j DB</button>
  </div>
  <div id="progress-bar-container" style="display:none;">
    <div id="progress-label"></div>
    <div id="progress-track"><div id="progress-fill"></div></div>
  </div>
  <div id="bottom-panel">
    <div id="panel-tabs">
      <button class="panel-tab active" data-tab="log">Log</button>
      <button class="panel-tab" data-tab="code">Details</button>
      <span class="log-actions">
        <button id="log-export" class="log-action-btn" title="Export log to file">Export</button>
        <button id="log-clear" class="log-action-btn" title="Clear log">Clear</button>
      </span>
      <button id="panel-toggle" title="Toggle panel">&#x25BC;</button>
    </div>
    <div id="tab-log" class="tab-content active">
      <div id="log-content"></div>
    </div>
    <div id="tab-code" class="tab-content">
      <div id="code-content"><span class="code-placeholder">Right-click a node to view code, summary, or investigation results.</span></div>
    </div>
  </div>
  <script src="${d3Uri}"></script>
  <script src="${jsUri}"></script>
</body>
</html>`;
}
