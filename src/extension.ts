import * as vscode from 'vscode';
import * as path from 'path';
import * as fs from 'fs';
import * as cp from 'child_process';
import { createWebviewPanel } from './webviewPanel';

const VDG_OUTPUT_DIR = 'vdg-output';

export function activate(context: vscode.ExtensionContext) {
  const disposable = vscode.commands.registerCommand(
    'vdg.generateChunks',
    async (uri?: vscode.Uri) => {
      let projectPath: string;

      if (uri) {
        const stat = await vscode.workspace.fs.stat(uri);
        if (stat.type === vscode.FileType.Directory) {
          projectPath = uri.fsPath;
        } else {
          projectPath = path.dirname(uri.fsPath);
        }
      } else {
        const folders = vscode.workspace.workspaceFolders;
        if (!folders || folders.length === 0) {
          vscode.window.showErrorMessage('No workspace folder open.');
          return;
        }
        projectPath = folders[0].uri.fsPath;
      }

      const projectName = path.basename(projectPath);
      const panel = createWebviewPanel(context, projectName);

      let disposed = false;
      panel.onDidDispose(() => { disposed = true; });

      const vdgDir = path.join(projectPath, VDG_OUTPUT_DIR);
      const chunkTreePath = path.join(vdgDir, 'chunk_tree.json');
      const edgesPath = path.join(vdgDir, 'edges.csv');
      const simEdgesPath = path.join(vdgDir, 'similarity_symbol_edges.csv');

      // Check if cached data exists — if so, load directly
      const hasCachedData = fs.existsSync(chunkTreePath);
      if (hasCachedData) {
        const hasSimEdges = fs.existsSync(simEdgesPath);
        panel.webview.postMessage({ type: 'loadCached', projectPath, hasSimEdges });
        // Check Neo4j container status on load
        checkNeo4jStatus(panel);
      }

      panel.webview.onDidReceiveMessage(
        async (message) => {
          if (message.type === 'start') {
            const maxNodeSize = message.maxNodeSize || 0;
            const preserveScoping = message.preserveScoping ?? true;
            runAnalysis(context, panel, projectPath, maxNodeSize, preserveScoping, () => disposed);
          } else if (message.type === 'requestCachedData') {
            // Load cached chunk tree and edges (including similarity edges)
            loadCachedData(panel, chunkTreePath, edgesPath, simEdgesPath, projectPath, () => disposed);
          } else if (message.type === 'regenerate') {
            // Clean up summaries from JSON and remove embedding edges
            if (fs.existsSync(chunkTreePath)) {
              try {
                const tree = JSON.parse(fs.readFileSync(chunkTreePath, 'utf-8'));
                (function stripSummaries(node: any) {
                  delete node.summary;
                  delete node.embedding;
                  if (node.children) { node.children.forEach(stripSummaries); }
                })(tree);
                fs.writeFileSync(chunkTreePath, JSON.stringify(tree, null, 2), 'utf-8');
              } catch { /* ignore parse errors */ }
            }
            if (fs.existsSync(simEdgesPath)) {
              fs.unlinkSync(simEdgesPath);
            }
            panel.webview.postMessage({ type: 'log', message: 'Cleared summaries and embedding edges.' });
          } else if (message.type === 'generateDeps') {
            // Remove stale embedding edges when regenerating summaries
            if (fs.existsSync(simEdgesPath)) {
              fs.unlinkSync(simEdgesPath);
              panel.webview.postMessage({ type: 'log', message: 'Removed stale embedding edges.' });
            }
            runSummarization(context, panel, chunkTreePath, () => disposed);
          } else if (message.type === 'generateEdges') {
            const topPercent = message.topPercent || 5;
            runSimilarityRecompute(context, panel, chunkTreePath, topPercent, simEdgesPath, projectPath, edgesPath, () => disposed);
          } else if (message.type === 'dumpNode') {
            // Dump a node subtree from the chunk_tree.json to a file
            try {
              const tree = JSON.parse(fs.readFileSync(chunkTreePath, 'utf-8'));
              const targetId = message.nodeId;
              function findNode(node: any): any {
                if (node.id === targetId) { return node; }
                if (node.children) {
                  for (const child of node.children) {
                    const found = findNode(child);
                    if (found) { return found; }
                  }
                }
                return null;
              }
              const target = findNode(tree);
              if (target) {
                const dumpPath = path.join(vdgDir, `node_dump_${targetId}.txt`);
                function dumpNode(node: any, depth: number, lines: string[]) {
                  const indent = '  '.repeat(depth);
                  const ch = node.children || [];
                  const leaf = ch.length === 0 ? ' [LEAF]' : '';
                  const pcInfo = node.pcColor ? ` pcColor=${node.pcColor}` : '';
                  const pcCond = node.pcCondition ? ` cond=${node.pcCondition}` : '';
                  lines.push(`${indent}${node.type || '?'}: ${node.name || '?'} L${node.lineStart || '?'}-${node.lineEnd || '?'} ch=${ch.length}${leaf}${pcInfo}${pcCond}`);
                  const content = node.content || '';
                  if (content) {
                    for (const cl of content.split('\n').slice(0, 30)) {
                      lines.push(`${indent}  | ${cl}`);
                    }
                    const totalLines = content.split('\n').length;
                    if (totalLines > 30) {
                      lines.push(`${indent}  | ... (${totalLines} lines total)`);
                    }
                  }
                  lines.push('');
                  for (const child of ch) {
                    dumpNode(child, depth + 1, lines);
                  }
                }
                const lines: string[] = [];
                dumpNode(target, 0, lines);
                fs.writeFileSync(dumpPath, lines.join('\n'), 'utf-8');
                panel.webview.postMessage({ type: 'log', message: `Node dump saved to ${dumpPath}` });
              } else {
                panel.webview.postMessage({ type: 'error', message: `Node ${targetId} not found in chunk tree.` });
              }
            } catch (err: any) {
              panel.webview.postMessage({ type: 'error', message: `Dump failed: ${err.message}` });
            }
          } else if (message.type === 'investigate') {
            runInvestigation(context, panel, chunkTreePath, message.nodeId, () => disposed);
          } else if (message.type === 'neo4jStart') {
            runNeo4jLoader(context, panel, 'start', vdgDir, () => disposed);
          } else if (message.type === 'neo4jStop') {
            runNeo4jLoader(context, panel, 'stop', vdgDir, () => disposed);
          } else if (message.type === 'neo4jCheckStatus') {
            checkNeo4jStatus(panel);
          } else if (message.type === 'exportLog') {
            const logUri = await vscode.window.showSaveDialog({
              filters: { 'Text File': ['txt'] },
              defaultUri: vscode.Uri.file(
                path.join(projectPath, 'log.txt')
              ),
            });
            if (logUri) {
              await vscode.workspace.fs.writeFile(logUri, Buffer.from(message.text, 'utf-8'));
              vscode.window.showInformationMessage(`Log exported to ${logUri.fsPath}`);
            }
          } else if (message.type === 'exportPng') {
            const saveUri = await vscode.window.showSaveDialog({
              filters: { 'PNG Image': ['png'] },
              defaultUri: vscode.Uri.file(
                path.join(
                  vscode.workspace.workspaceFolders?.[0]?.uri.fsPath || '',
                  `${projectName}-ast.png`
                )
              ),
            });
            if (saveUri) {
              const dataUrl: string = message.dataUrl;
              const base64 = dataUrl.replace(/^data:image\/png;base64,/, '');
              const buffer = Buffer.from(base64, 'base64');
              await vscode.workspace.fs.writeFile(saveUri, buffer);
              vscode.window.showInformationMessage(`PNG exported to ${saveUri.fsPath}`);
            }
          }
        },
        undefined,
        context.subscriptions
      );
    }
  );

  context.subscriptions.push(disposable);
}

function loadEdgesFromCsv(csvPath: string): any[] {
  const edges: any[] = [];
  if (!fs.existsSync(csvPath)) { return edges; }
  const lines = fs.readFileSync(csvPath, 'utf-8').split('\n');
  for (let i = 1; i < lines.length; i++) {
    const parts = lines[i].split(',');
    if (parts.length >= 9) {
      const edge: any = {
        edgeType: parts[0],
        srcId: parts[1],
        srcName: parts[2],
        srcFile: parts[3],
        srcLine: parseInt(parts[4]) || -1,
        dstId: parts[5],
        dstName: parts[6],
        dstFile: parts[7],
        dstLine: parseInt(parts[8]) || -1,
      };
      if (parts.length >= 10 && parts[9]) {
        edge.similarity = parseFloat(parts[9]) || 0;
      }
      if (parts.length >= 11 && parts[10]) {
        edge.symbol = parts[10];
      }
      edges.push(edge);
    }
  }
  return edges;
}

function loadCachedData(
  panel: vscode.WebviewPanel,
  chunkTreePath: string,
  edgesPath: string,
  simEdgesPath: string,
  projectPath: string,
  isDisposed: () => boolean
) {
  if (isDisposed()) { return; }

  try {
    const treeData = JSON.parse(fs.readFileSync(chunkTreePath, 'utf-8'));

    // Load source files for code display
    const sourceFiles: Record<string, string> = {};
    const files = new Set<string>();
    (function findFiles(node: any) {
      if (node.file) { files.add(node.file); }
      if (node.children) { node.children.forEach(findFiles); }
    })(treeData);
    for (const relPath of files) {
      const fullPath = path.join(projectPath, relPath);
      if (fs.existsSync(fullPath)) {
        sourceFiles[relPath] = fs.readFileSync(fullPath, 'utf-8');
      }
    }

    // Load structural edges and similarity edges
    const edges = loadEdgesFromCsv(edgesPath);
    const simEdges = loadEdgesFromCsv(simEdgesPath);
    const allEdges = edges.concat(simEdges);

    // Count LOC
    let totalLoc = 0;
    for (const content of Object.values(sourceFiles)) {
      totalLoc += content.split('\n').length;
    }

    // Detect if any node has a summary
    let hasSummaries = false;
    (function checkSummaries(node: any) {
      if (node.summary) { hasSummaries = true; }
      if (!hasSummaries && node.children) { node.children.forEach(checkSummaries); }
    })(treeData);

    panel.webview.postMessage({ type: 'log', message: `Loaded cached data: ${edges.length} structural edges, ${simEdges.length} embedding edges.` });
    panel.webview.postMessage({
      type: 'data',
      payload: treeData,
      sourceFiles: sourceFiles,
      edges: allEdges,
      totalLoc: totalLoc,
      hasSummaries: hasSummaries,
      hasSimEdges: simEdges.length > 0,
    });
    panel.webview.postMessage({ type: 'done' });
  } catch (err: any) {
    panel.webview.postMessage({
      type: 'error',
      message: `Failed to load cached data: ${err.message}`,
    });
  }
}

function runAnalysis(
  context: vscode.ExtensionContext,
  panel: vscode.WebviewPanel,
  projectPath: string,
  maxNodeSize: number,
  preserveScoping: boolean,
  isDisposed: () => boolean
) {
  const pythonCmd = process.platform === 'win32' ? 'python' : 'python3';
  const scriptPath = path.join(context.extensionPath, 'tools', 'analyze.py');

  const args = [scriptPath, projectPath];
  if (maxNodeSize > 0) {
    args.push('--max-node-size', String(maxNodeSize));
  }
  if (preserveScoping) {
    args.push('--preserve-scoping');
  }

  const child = cp.spawn(pythonCmd, args, {
    cwd: context.extensionPath,
  });

  panel.onDidDispose(() => { child.kill(); });

  let stdoutBuffer = '';

  child.stdout.on('data', (data: Buffer) => {
    if (isDisposed()) { return; }
    stdoutBuffer += data.toString();

    const lines = stdoutBuffer.split('\n');
    stdoutBuffer = lines.pop() || '';

    for (const line of lines) {
      if (!line.trim()) { continue; }
      try {
        const msg = JSON.parse(line);
        if (msg.type === 'log') {
          panel.webview.postMessage({ type: 'log', message: msg.message });
        } else if (msg.type === 'result') {
          panel.webview.postMessage({
            type: 'data',
            payload: msg.data,
            sourceFiles: msg.sourceFiles || {},
            edges: msg.edges || [],
            totalLoc: msg.totalLoc || 0,
          });
        } else if (msg.type === 'error') {
          panel.webview.postMessage({ type: 'error', message: msg.message });
        }
      } catch {
        panel.webview.postMessage({ type: 'log', message: line });
      }
    }
  });

  child.stderr.on('data', (data: Buffer) => {
    if (isDisposed()) { return; }
    const text = data.toString().trim();
    if (text) {
      panel.webview.postMessage({ type: 'log', message: `[stderr] ${text}` });
    }
  });

  child.on('close', (code) => {
    if (isDisposed()) { return; }
    if (code !== 0) {
      panel.webview.postMessage({
        type: 'error',
        message: `Analysis process exited with code ${code}`,
      });
    }
    panel.webview.postMessage({ type: 'done' });
  });

  child.on('error', (err) => {
    if (isDisposed()) { return; }
    panel.webview.postMessage({
      type: 'error',
      message: `Failed to start analysis: ${err.message}. Is python3 installed?`,
    });
  });
}

function runSummarization(
  context: vscode.ExtensionContext,
  panel: vscode.WebviewPanel,
  chunkTreePath: string,
  isDisposed: () => boolean
) {
  const pythonCmd = process.platform === 'win32' ? 'python' : 'python3';
  const scriptPath = path.join(context.extensionPath, 'tools', 'summarize.py');

  panel.webview.postMessage({ type: 'log', message: 'Starting summary generation...' });

  const child = cp.spawn(pythonCmd, [scriptPath, chunkTreePath]);

  panel.onDidDispose(() => { child.kill(); });

  let stdoutBuffer = '';

  child.stdout.on('data', (data: Buffer) => {
    if (isDisposed()) { return; }
    stdoutBuffer += data.toString();

    const lines = stdoutBuffer.split('\n');
    stdoutBuffer = lines.pop() || '';

    for (const line of lines) {
      if (!line.trim()) { continue; }
      try {
        const msg = JSON.parse(line);
        if (msg.type === 'log') {
          panel.webview.postMessage({ type: 'log', message: msg.message });
        } else if (msg.type === 'progress') {
          panel.webview.postMessage({ type: 'progress', phase: msg.phase, current: msg.current, total: msg.total });
        } else if (msg.type === 'done') {
          panel.webview.postMessage({ type: 'log', message: 'Summary generation complete.' });
          panel.webview.postMessage({ type: 'summariesDone' });
        } else if (msg.type === 'error') {
          panel.webview.postMessage({ type: 'error', message: msg.message });
        }
      } catch {
        panel.webview.postMessage({ type: 'log', message: line });
      }
    }
  });

  child.stderr.on('data', (data: Buffer) => {
    if (isDisposed()) { return; }
    const text = data.toString().trim();
    if (text) {
      panel.webview.postMessage({ type: 'log', message: `[summarize stderr] ${text}` });
    }
  });

  child.on('close', (code) => {
    if (isDisposed()) { return; }
    if (code !== 0) {
      panel.webview.postMessage({
        type: 'error',
        message: `Summarization exited with code ${code}`,
      });
    }
  });

  child.on('error', (err) => {
    if (isDisposed()) { return; }
    panel.webview.postMessage({
      type: 'error',
      message: `Failed to start summarization: ${err.message}`,
    });
  });
}

function runSimilarityRecompute(
  context: vscode.ExtensionContext,
  panel: vscode.WebviewPanel,
  chunkTreePath: string,
  topPercent: number,
  simEdgesPath: string,
  projectPath: string,
  edgesPath: string,
  isDisposed: () => boolean
) {
  const pythonCmd = process.platform === 'win32' ? 'python' : 'python3';
  const scriptPath = path.join(context.extensionPath, 'tools', 'compute_similarity.py');

  panel.webview.postMessage({ type: 'log', message: `Recomputing similarity edges (top ${topPercent}%)...` });

  const child = cp.spawn(pythonCmd, [scriptPath, chunkTreePath, '--top-percent', String(topPercent)]);

  panel.onDidDispose(() => { child.kill(); });

  let stdoutBuffer = '';

  child.stdout.on('data', (data: Buffer) => {
    if (isDisposed()) { return; }
    stdoutBuffer += data.toString();
    const lines = stdoutBuffer.split('\n');
    stdoutBuffer = lines.pop() || '';
    for (const line of lines) {
      if (!line.trim()) { continue; }
      try {
        const msg = JSON.parse(line);
        if (msg.type === 'log') {
          panel.webview.postMessage({ type: 'log', message: msg.message });
        } else if (msg.type === 'progress') {
          panel.webview.postMessage({ type: 'progress', phase: msg.phase, current: msg.current, total: msg.total });
        } else if (msg.type === 'done') {
          panel.webview.postMessage({ type: 'edgesDone' });
        } else if (msg.type === 'error') {
          panel.webview.postMessage({ type: 'error', message: msg.message });
        }
      } catch {
        panel.webview.postMessage({ type: 'log', message: line });
      }
    }
  });

  child.stderr.on('data', (data: Buffer) => {
    if (isDisposed()) { return; }
    const text = data.toString().trim();
    if (text) {
      panel.webview.postMessage({ type: 'log', message: `[similarity stderr] ${text}` });
    }
  });

  child.on('close', (code) => {
    if (isDisposed()) { return; }
    if (code !== 0) {
      panel.webview.postMessage({ type: 'error', message: `Similarity computation exited with code ${code}` });
    }
  });

  child.on('error', (err) => {
    if (isDisposed()) { return; }
    panel.webview.postMessage({ type: 'error', message: `Failed to start similarity computation: ${err.message}` });
  });
}

function checkNeo4jStatus(panel: vscode.WebviewPanel) {
  cp.exec('docker ps --filter "name=^/vdg-neo4j$" --format "{{.Names}}"', (err, stdout) => {
    const running = !err && stdout.trim() === 'vdg-neo4j';
    panel.webview.postMessage({ type: 'neo4jStatus', running });
  });
}

function runNeo4jLoader(
  context: vscode.ExtensionContext,
  panel: vscode.WebviewPanel,
  action: string,
  vdgDir: string,
  isDisposed: () => boolean
) {
  const pythonCmd = process.platform === 'win32' ? 'python' : 'python3';
  const scriptPath = path.join(context.extensionPath, 'tools', 'neo4j_loader.py');

  const args = action === 'start'
    ? [scriptPath, 'start', vdgDir]
    : [scriptPath, 'stop'];

  panel.webview.postMessage({ type: 'log', message: `Neo4j: ${action}ing...` });

  const child = cp.spawn(pythonCmd, args);

  panel.onDidDispose(() => { child.kill(); });

  let stdoutBuffer = '';

  child.stdout.on('data', (data: Buffer) => {
    if (isDisposed()) { return; }
    stdoutBuffer += data.toString();
    const lines = stdoutBuffer.split('\n');
    stdoutBuffer = lines.pop() || '';
    for (const line of lines) {
      if (!line.trim()) { continue; }
      try {
        const msg = JSON.parse(line);
        if (msg.type === 'log') {
          panel.webview.postMessage({ type: 'log', message: msg.message });
        } else if (msg.type === 'done') {
          const running = msg.action === 'started';
          panel.webview.postMessage({ type: 'neo4jStatus', running });
          panel.webview.postMessage({ type: 'neo4jDone' });
        } else if (msg.type === 'error') {
          panel.webview.postMessage({ type: 'error', message: msg.message });
        }
      } catch {
        panel.webview.postMessage({ type: 'log', message: line });
      }
    }
  });

  child.stderr.on('data', (data: Buffer) => {
    if (isDisposed()) { return; }
    const text = data.toString().trim();
    if (text) {
      panel.webview.postMessage({ type: 'log', message: `[neo4j] ${text}` });
    }
  });

  child.on('close', (code) => {
    if (isDisposed()) { return; }
    if (code !== 0) {
      panel.webview.postMessage({ type: 'error', message: `Neo4j loader exited with code ${code}` });
    }
  });

  child.on('error', (err) => {
    if (isDisposed()) { return; }
    panel.webview.postMessage({ type: 'error', message: `Failed to run neo4j_loader: ${err.message}` });
  });
}

function runInvestigation(
  context: vscode.ExtensionContext,
  panel: vscode.WebviewPanel,
  chunkTreePath: string,
  nodeId: string,
  isDisposed: () => boolean
) {
  const pythonCmd = process.platform === 'win32' ? 'python' : 'python3';
  const scriptPath = path.join(context.extensionPath, 'tools', 'investigate.py');

  panel.webview.postMessage({ type: 'log', message: `Starting investigation for node ${nodeId}...` });

  const child = cp.spawn(pythonCmd, [scriptPath, chunkTreePath, nodeId]);

  panel.onDidDispose(() => { child.kill(); });

  let stdoutBuffer = '';

  child.stdout.on('data', (data: Buffer) => {
    if (isDisposed()) { return; }
    stdoutBuffer += data.toString();
    const lines = stdoutBuffer.split('\n');
    stdoutBuffer = lines.pop() || '';
    for (const line of lines) {
      if (!line.trim()) { continue; }
      try {
        const msg = JSON.parse(line);
        if (msg.type === 'log') {
          panel.webview.postMessage({ type: 'log', message: msg.message });
        } else if (msg.type === 'tool') {
          panel.webview.postMessage({ type: 'log', message: `Tool: ${msg.name}(${msg.args}) → ${msg.resultCount} results` });
        } else if (msg.type === 'result') {
          // Save investigation to JSON
          try {
            const tree = JSON.parse(fs.readFileSync(chunkTreePath, 'utf-8'));
            (function saveInvestigation(node: any): boolean {
              if (node.id === nodeId) {
                node.investigation = msg.analysis;
                return true;
              }
              if (node.children) {
                for (const child of node.children) {
                  if (saveInvestigation(child)) { return true; }
                }
              }
              return false;
            })(tree);
            fs.writeFileSync(chunkTreePath, JSON.stringify(tree, null, 2), 'utf-8');
          } catch { /* ignore save errors */ }
          panel.webview.postMessage({
            type: 'investigationResult', analysis: msg.analysis, nodeId,
            visitedNodes: msg.visitedNodes || [], visitedCount: msg.visitedCount || 0,
            toolCalls: msg.toolCalls || 0,
          });
        } else if (msg.type === 'error') {
          panel.webview.postMessage({ type: 'error', message: msg.message });
        }
      } catch {
        panel.webview.postMessage({ type: 'log', message: line });
      }
    }
  });

  child.stderr.on('data', (data: Buffer) => {
    if (isDisposed()) { return; }
    const text = data.toString().trim();
    if (text) {
      panel.webview.postMessage({ type: 'log', message: `[investigate] ${text}` });
    }
  });

  child.on('close', (code) => {
    if (isDisposed()) { return; }
    panel.webview.postMessage({ type: 'investigationDone' });
    if (code !== 0) {
      panel.webview.postMessage({ type: 'error', message: `Investigation exited with code ${code}` });
    }
  });

  child.on('error', (err) => {
    if (isDisposed()) { return; }
    panel.webview.postMessage({ type: 'error', message: `Failed to start investigation: ${err.message}` });
  });
}

export function deactivate() {}
