(function () {
  // eslint-disable-next-line no-undef
  const vscode = acquireVsCodeApi();

  // --- Config screen: Start button ---
  document.getElementById('start-analysis').addEventListener('click', function () {
    var maxNodeSize = parseInt(document.getElementById('max-node-size').value, 10) || 0;
    var preserveScoping = document.getElementById('preserve-scoping').checked;
    vscode.postMessage({ type: 'start', maxNodeSize: maxNodeSize, preserveScoping: preserveScoping });
    setBusy(true);
    document.getElementById('config-screen').style.display = 'none';
    document.getElementById('tree-container').style.display = '';
  });

  // --- Action bar buttons ---
  document.getElementById('btn-regenerate').addEventListener('click', function () {
    // Notify extension to clean up summaries and edges
    vscode.postMessage({ type: 'regenerate' });
    _hasSummaries = false;
    _hasSimEdges = false;
    // Show config screen, hide tree
    document.getElementById('config-screen').style.display = '';
    document.getElementById('tree-container').style.display = 'none';
    var overlay = document.getElementById('loading-overlay');
    if (overlay) { overlay.style.display = ''; }
  });

  document.getElementById('btn-generate-deps').addEventListener('click', function () {
    if (!_hasChunkData || _isBusy) { return; }
    setBusy(true);
    vscode.postMessage({ type: 'generateDeps' });
    appendLog('Requesting summary generation...');
  });

  document.getElementById('btn-generate-edges').addEventListener('click', function () {
    if (!_hasSummaries || _isBusy) { return; }
    setBusy(true);
    var topPercent = parseFloat(document.getElementById('similarity-threshold').value) || 5;
    vscode.postMessage({ type: 'generateEdges', topPercent: topPercent });
    appendLog('Generating embedding edges (top ' + topPercent + '%)...');
  });

  document.getElementById('btn-neo4j').addEventListener('click', function () {
    if (_isBusy) { return; }
    if (_neo4jRunning) {
      setBusy(true);
      vscode.postMessage({ type: 'neo4jStop' });
    } else if (_hasStructuralEdges && _hasSimEdges) {
      setBusy(true);
      vscode.postMessage({ type: 'neo4jStart' });
    }
  });

  // Track state for enabling/disabling buttons
  let _hasChunkData = false;
  let _hasSummaries = false;
  let _hasSimEdges = false;
  let _hasStructuralEdges = false;
  let _isBusy = false;
  let _neo4jRunning = false;

  function setBusy(busy) {
    _isBusy = busy;
    updateButtonStates();
  }

  function updateButtonStates() {
    var regenBtn = document.getElementById('btn-regenerate');
    var depsBtn = document.getElementById('btn-generate-deps');
    var edgesBtn = document.getElementById('btn-generate-edges');

    if (_isBusy) {
      regenBtn.disabled = true;
      depsBtn.disabled = true;
      edgesBtn.disabled = true;
      return;
    }

    regenBtn.disabled = false;

    if (_hasChunkData) {
      depsBtn.disabled = false;
      depsBtn.textContent = _hasSummaries ? 'Regenerate Summaries' : 'Generate Summaries';
      depsBtn.title = _hasSummaries ? 'Regenerate summaries for all leaf nodes' : 'Generate summaries for all leaf nodes';
    } else {
      depsBtn.disabled = true;
      depsBtn.textContent = 'Generate Summaries';
      depsBtn.title = 'Generate chunks first';
    }

    if (_hasSummaries) {
      edgesBtn.disabled = false;
      edgesBtn.textContent = _hasSimEdges ? 'Regenerate Edges' : 'Generate Edges';
      edgesBtn.title = _hasSimEdges ? 'Recompute embedding edges with the given threshold' : 'Generate embedding edges from summaries';
    } else {
      edgesBtn.disabled = true;
      edgesBtn.textContent = 'Generate Edges';
      edgesBtn.title = 'Generate summaries first';
    }

    var neo4jBtn = document.getElementById('btn-neo4j');
    if (_neo4jRunning) {
      neo4jBtn.disabled = false;
      neo4jBtn.textContent = 'Stop Neo4j DB';
      neo4jBtn.title = 'Stop and remove the Neo4j container';
    } else if (_hasStructuralEdges && _hasSimEdges) {
      neo4jBtn.disabled = false;
      neo4jBtn.textContent = 'Start Neo4j DB';
      neo4jBtn.title = 'Start Neo4j and load all data';
    } else {
      neo4jBtn.disabled = true;
      neo4jBtn.textContent = 'Start Neo4j DB';
      neo4jBtn.title = 'Generate all edges first (structural + embedding)';
    }
  }

  const PAD_X = 12;
  const PAD_Y = 6;
  const ROOT_EXTRA_PAD = 4;

  // Collapse state
  const collapsedNames = new Set();

  // Original data for re-renders
  let _rootData = null;

  // Zoom, SVG, bounds
  let _zoom = null;
  let _svg = null;
  let _treeBounds = null;
  let _rootPos = null;

  // Node lookup for zoom
  let _nodesByPath = {};

  // Source file snapshot — {filepath: content}
  let _sourceFiles = {};

  // Suppress auto-zoom on collapse toggle
  let _suppressZoomToFit = false;

  // Large model detection
  let _isLargeModel = false;
  let _initialCollapsedNames = new Set();

  // Stats
  let _totalLoc = 0;

  // Edge data
  let _allEdges = [];        // all edges from backend
  let _activeEdgeChunkId = null;  // chunk currently showing edges
  let _nodesById = {};

  // Unique path generator — nodes may share names, so use path from root
  function buildPathMap(node, parentPath) {
    const myPath = parentPath ? parentPath + ' > ' + node.name : node.name;
    node._path = myPath;
    if (node.children) {
      // Disambiguate children with same name by appending index
      const nameCount = {};
      node.children.forEach(function (child) {
        const key = child.name;
        nameCount[key] = (nameCount[key] || 0) + 1;
      });
      const nameIndex = {};
      node.children.forEach(function (child) {
        const key = child.name;
        nameIndex[key] = (nameIndex[key] || 0) + 1;
        const suffix = nameCount[key] > 1 ? ' #' + nameIndex[key] : '';
        buildPathMap(child, myPath);
        if (suffix) {
          child._path += suffix;
        }
      });
    }
  }

  // Node type → color class mapping
  // Only structural, scope, and merged types get special colors.
  // Everything else is default (white with black border).
  const TYPE_COLORS = {
    PROJECT: 'node-project',
    DIRECTORY: 'node-directory',
    FILE: 'node-file',
    merged: 'node-merged',
    scope_open: 'node-scope-open',
    scope_close: 'node-scope-close',
    PC: 'node-pc',
    if_pc: 'node-pc-alt',
    elif_pc: 'node-pc-alt',
    else_pc: 'node-pc-alt',
    pc_code: 'node-default',
  };

  function getNodeColorClass(type) {
    return TYPE_COLORS[type] || 'node-default';
  }

  // --- Bottom panel with tabs ---
  const logContent = document.getElementById('log-content');
  const codeContent = document.getElementById('code-content');
  let panelCollapsed = false;

  // Tab switching
  document.querySelectorAll('.panel-tab').forEach(function (tab) {
    tab.addEventListener('click', function () {
      var tabName = tab.getAttribute('data-tab');
      document.querySelectorAll('.panel-tab').forEach(function (t) { t.classList.remove('active'); });
      document.querySelectorAll('.tab-content').forEach(function (tc) { tc.classList.remove('active'); });
      tab.classList.add('active');
      document.getElementById('tab-' + tabName).classList.add('active');
    });
  });

  // Log clear button
  document.getElementById('log-clear').addEventListener('click', function () {
    logContent.innerHTML = '';
  });

  // Log export button
  document.getElementById('log-export').addEventListener('click', function () {
    var lines = [];
    logContent.querySelectorAll('.log-line').forEach(function (el) {
      lines.push(el.textContent);
    });
    vscode.postMessage({ type: 'exportLog', text: lines.join('\n') });
  });

  // Panel toggle (collapse/expand)
  document.getElementById('panel-toggle').addEventListener('click', function () {
    panelCollapsed = !panelCollapsed;
    document.querySelectorAll('.tab-content').forEach(function (tc) {
      tc.style.display = panelCollapsed ? 'none' : '';
    });
    document.getElementById('panel-toggle').textContent = panelCollapsed ? '\u25B6' : '\u25BC';
  });

  // Walk up the D3 hierarchy to find the ancestor FILE node name
  function findAncestorFile(d3node) {
    var cur = d3node;
    while (cur) {
      if (cur.data && cur.data.type === 'FILE') {
        return cur.data.name;
      }
      cur = cur.parent;
    }
    return '';
  }

  // Also check original _rootData tree for file field
  function findNodeFileFromData(path, node) {
    if (node._path === path) { return node.file || ''; }
    if (node.children) {
      for (var i = 0; i < node.children.length; i++) {
        var r = findNodeFileFromData(path, node.children[i]);
        if (r) { return r; }
      }
    }
    return '';
  }

  function escapeHtml(text) {
    return text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  function showCodeForNode(nodeData, d3node) {
    var nodeName = nodeData.name || '';
    var nodeId = nodeData.id || '';
    var content = nodeData.content;

    // If content field exists, display it directly
    if (content != null) {
      var file = nodeData.file || '';
      var lineStart = nodeData.lineStart || -1;
      var lineEnd = nodeData.lineEnd || -1;

      var location = '';
      if (file) {
        location = file;
        if (lineStart > 0) {
          location += ':' + lineStart;
          if (lineEnd > lineStart) { location += '-' + lineEnd; }
        }
      }

      var headerText = nodeName;
      if (nodeId) { headerText += '  [' + nodeId + ']'; }
      if (location) { headerText += '  \u2014  ' + location; }

      var headerDiv = document.createElement('div');
      headerDiv.className = 'code-header';
      headerDiv.textContent = headerText;

      var bodyDiv = document.createElement('div');
      bodyDiv.className = 'code-body';

      if (content === '') {
        bodyDiv.innerHTML = '<span class="code-placeholder">(pass-through node — no additional content)</span>';
      } else {
        // Render content with placeholder highlighting
        var contentLines = content.split('\n');
        var htmlParts = [];
        for (var i = 0; i < contentLines.length; i++) {
          var rawLine = contentLines[i];
          // Highlight <placeholder_id> patterns
          var escapedLine = escapeHtml(rawLine);
          escapedLine = escapedLine.replace(/&lt;(\w+)&gt;/g, '<span class="code-placeholder-tag">&lt;$1&gt;</span>');
          htmlParts.push(escapedLine);
        }
        bodyDiv.innerHTML = htmlParts.join('\n');
      }

      codeContent.innerHTML = '';
      codeContent.appendChild(headerDiv);
      codeContent.appendChild(bodyDiv);
    } else {
      // Fallback: no content field, use legacy source lookup
      var file = nodeData.file || '';
      var lineStart = nodeData.lineStart || -1;
      var lineEnd = nodeData.lineEnd || -1;
      var colStart = nodeData.colStart != null ? nodeData.colStart : -1;
      var colEnd = nodeData.colEnd != null ? nodeData.colEnd : -1;

      if (!file && d3node) {
        file = findAncestorFile(d3node);
      }
      if (!file && _rootData && nodeData._path) {
        file = findNodeFileFromData(nodeData._path, _rootData);
      }

      if (!file || !_sourceFiles[file]) {
        codeContent.innerHTML = '<span class="code-placeholder">' +
          (file ? 'Source file "' + file + '" not found in snapshot.' : 'Cannot determine source file for this node.') +
          '</span>';
        return;
      }

      var allLines = _sourceFiles[file].split('\n');
      var endLine = lineEnd > 0 ? lineEnd : lineStart;

      if (lineStart <= 0) {
        codeContent.innerHTML = '<span class="code-placeholder">No line information for this node.</span>';
        return;
      }

      var startIdx = Math.max(0, lineStart - 1);
      var endIdx = Math.min(allLines.length, endLine);
      var displayLines = allLines.slice(startIdx, endIdx);
      var hasColInfo = colStart >= 0 && colEnd >= 0;
      var htmlParts = [];

      for (var i = 0; i < displayLines.length; i++) {
        var lineNum = startIdx + i + 1;
        var rawLine = displayLines[i];
        var lineNumStr = '<span class="code-linenum">' + lineNum + '</span> ';
        htmlParts.push(lineNumStr + '<span class="code-highlight">' + escapeHtml(rawLine) + '</span>');
      }

      var location = file + ':' + lineStart;
      if (endLine > lineStart) { location += '-' + endLine; }

      var headerDiv = document.createElement('div');
      headerDiv.className = 'code-header';
      headerDiv.textContent = nodeName + '  \u2014  ' + location;

      var bodyDiv = document.createElement('div');
      bodyDiv.className = 'code-body';
      bodyDiv.innerHTML = htmlParts.join('\n');

      codeContent.innerHTML = '';
      codeContent.appendChild(headerDiv);
      codeContent.appendChild(bodyDiv);
    }

    // Switch to code tab
    document.querySelectorAll('.panel-tab').forEach(function (t) { t.classList.remove('active'); });
    document.querySelectorAll('.tab-content').forEach(function (tc) { tc.classList.remove('active'); });
    document.querySelector('.panel-tab[data-tab="code"]').classList.add('active');
    document.getElementById('tab-code').classList.add('active');

    // Expand panel if collapsed
    if (panelCollapsed) {
      panelCollapsed = false;
      document.querySelectorAll('.tab-content').forEach(function (tc) {
        tc.style.display = '';
      });
      document.getElementById('panel-toggle').textContent = '\u25BC';
    }
  }

  function showInvestigationResult(nodeId, analysis, visitedNodes, visitedCount, toolCalls) {
    var headerDiv = document.createElement('div');
    headerDiv.className = 'code-header';
    var statsText = '';
    if (visitedCount) {
      statsText = '  (' + visitedCount + ' nodes visited, ' + toolCalls + ' tool calls)';
    }
    headerDiv.textContent = 'Impact Analysis \u2014 ' + nodeId + statsText;

    var bodyDiv = document.createElement('div');
    bodyDiv.className = 'code-body';
    bodyDiv.style.whiteSpace = 'pre-wrap';
    bodyDiv.textContent = analysis || '(no analysis produced)';

    // Visited nodes section
    if (visitedNodes && visitedNodes.length > 0) {
      var visitedDiv = document.createElement('div');
      visitedDiv.className = 'code-header';
      visitedDiv.style.marginTop = '12px';
      visitedDiv.textContent = 'Nodes Explored (' + visitedNodes.length + ')';

      var visitedBody = document.createElement('div');
      visitedBody.className = 'code-body';
      visitedBody.style.fontSize = '11px';
      visitedBody.style.color = '#666';
      var visitedLines = visitedNodes.map(function (v) {
        return v.nodeId + '  ' + v.name + '  (' + (v.file || 'n/a') + ')';
      });
      visitedBody.textContent = visitedLines.join('\n');
    }

    codeContent.innerHTML = '';
    codeContent.appendChild(headerDiv);
    codeContent.appendChild(bodyDiv);
    if (visitedNodes && visitedNodes.length > 0) {
      codeContent.appendChild(visitedDiv);
      codeContent.appendChild(visitedBody);
    }

    // Switch to details tab and expand panel
    document.querySelectorAll('.panel-tab').forEach(function (t) { t.classList.remove('active'); });
    document.querySelectorAll('.tab-content').forEach(function (tc) { tc.classList.remove('active'); });
    document.querySelector('.panel-tab[data-tab="code"]').classList.add('active');
    document.getElementById('tab-code').classList.add('active');
    if (panelCollapsed) {
      panelCollapsed = false;
      document.querySelectorAll('.tab-content').forEach(function (tc) { tc.style.display = ''; });
      document.getElementById('panel-toggle').textContent = '\u25BC';
    }
    appendLog('Impact analysis ready for node ' + nodeId +
      (visitedCount ? ' (' + visitedCount + ' nodes visited)' : '') + '.');
  }

  function showSummaryForNode(nodeData) {
    var nodeName = nodeData.name || '';
    var nodeId = nodeData.id || '';
    var summary = nodeData.summary || '';

    var headerText = nodeName;
    if (nodeId) { headerText += '  [' + nodeId + ']'; }

    var headerDiv = document.createElement('div');
    headerDiv.className = 'code-header';
    headerDiv.textContent = 'Summary \u2014 ' + headerText;

    var bodyDiv = document.createElement('div');
    bodyDiv.className = 'code-body';
    bodyDiv.textContent = summary || '(no summary available)';

    codeContent.innerHTML = '';
    codeContent.appendChild(headerDiv);
    codeContent.appendChild(bodyDiv);

    // Switch to code tab
    document.querySelectorAll('.panel-tab').forEach(function (t) { t.classList.remove('active'); });
    document.querySelectorAll('.tab-content').forEach(function (tc) { tc.classList.remove('active'); });
    document.querySelector('.panel-tab[data-tab="code"]').classList.add('active');
    document.getElementById('tab-code').classList.add('active');
    if (panelCollapsed) {
      panelCollapsed = false;
      document.querySelectorAll('.tab-content').forEach(function (tc) { tc.style.display = ''; });
      document.getElementById('panel-toggle').textContent = '\u25BC';
    }
  }

  function appendLog(message) {
    const line = document.createElement('div');
    line.className = 'log-line';
    const time = new Date().toLocaleTimeString();
    line.textContent = '[' + time + '] ' + message;
    logContent.appendChild(line);
    logContent.scrollTop = logContent.scrollHeight;
  }

  function appendError(message) {
    const line = document.createElement('div');
    line.className = 'log-line log-error';
    const time = new Date().toLocaleTimeString();
    line.textContent = '[' + time + '] ERROR: ' + message;
    logContent.appendChild(line);
    logContent.scrollTop = logContent.scrollHeight;
  }

  function showProgress(phase, current, total) {
    var container = document.getElementById('progress-bar-container');
    var label = document.getElementById('progress-label');
    var fill = document.getElementById('progress-fill');
    container.style.display = '';
    var pct = total > 0 ? Math.round((current / total) * 100) : 0;
    var phaseLabels = {
      'summaries': 'Generating summaries',
      'symbols': 'Extracting symbols',
      'embedding': 'Generating embeddings',
      'similarity': 'Computing similarity'
    };
    var phaseLabel = phaseLabels[phase] || phase;
    label.textContent = phaseLabel + ': ' + current + '/' + total + ' (' + pct + '%)';
    fill.style.width = pct + '%';
  }

  function hideProgress() {
    document.getElementById('progress-bar-container').style.display = 'none';
    document.getElementById('progress-fill').style.width = '0%';
  }

  function hasSummariesInTree(node) {
    if (!node) { return false; }
    if (node.summary) { return true; }
    if (node.children) {
      for (var i = 0; i < node.children.length; i++) {
        if (hasSummariesInTree(node.children[i])) { return true; }
      }
    }
    return false;
  }

  // --- Message handler ---
  window.addEventListener('message', function (event) {
    var message = event.data;
    if (message.type === 'log') {
      appendLog(message.message);
    } else if (message.type === 'error') {
      setBusy(false);
      hideProgress();
      appendError(message.message);
    } else if (message.type === 'progress') {
      showProgress(message.phase, message.current, message.total);
    } else if (message.type === 'data') {
      // Hide loading overlay
      var overlay = document.getElementById('loading-overlay');
      if (overlay) { overlay.style.display = 'none'; }
      // Store source file snapshot, edges, and LOC
      _sourceFiles = message.sourceFiles || {};
      _allEdges = message.edges || [];
      _totalLoc = message.totalLoc || 0;
      _hasChunkData = true;
      _hasSummaries = !!message.hasSummaries || hasSummariesInTree(message.payload);
      _hasSimEdges = !!message.hasSimEdges || _allEdges.some(function (e) { return e.edgeType === 'EMBEDDING' || e.edgeType === 'PROVIDES'; });
      _hasStructuralEdges = _allEdges.some(function (e) { return e.edgeType !== 'EMBEDDING' && e.edgeType !== 'PROVIDES'; });
      updateButtonStates();
      appendLog('Data received (' + Object.keys(_sourceFiles).length + ' source files, ' + _allEdges.length + ' edges, ' + _totalLoc + ' LOC). Rendering tree...');
      render(message.payload);
      appendLog('Tree rendered.');
    } else if (message.type === 'loadCached') {
      // Cached data exists — skip config screen, show tree, request data
      document.getElementById('config-screen').style.display = 'none';
      document.getElementById('tree-container').style.display = '';
      appendLog('Loading cached chunk data...');
      vscode.postMessage({ type: 'requestCachedData' });
    } else if (message.type === 'summariesDone') {
      _hasSummaries = true;
      setBusy(false);
      hideProgress();
      appendLog('Summaries generated. Reloading tree...');
      vscode.postMessage({ type: 'requestCachedData' });
    } else if (message.type === 'edgesDone') {
      _hasSimEdges = true;
      setBusy(false);
      hideProgress();
      appendLog('Embedding edges generated. Reloading...');
      vscode.postMessage({ type: 'requestCachedData' });
    } else if (message.type === 'investigationResult') {
      // Save in memory so "Show investigation" works without reload
      if (_rootData) {
        (function saveInMem(node) {
          if (node.id === message.nodeId) { node.investigation = message.analysis; return; }
          if (node.children) { node.children.forEach(saveInMem); }
        })(_rootData);
        // Re-render so D3 nodes pick up the investigation field
        _suppressZoomToFit = true;
        renderTree(_rootData);
        _suppressZoomToFit = false;
      }
      showInvestigationResult(message.nodeId, message.analysis,
        message.visitedNodes, message.visitedCount, message.toolCalls);
    } else if (message.type === 'investigationDone') {
      setBusy(false);
    } else if (message.type === 'neo4jStatus') {
      _neo4jRunning = !!message.running;
      updateButtonStates();
    } else if (message.type === 'neo4jDone') {
      setBusy(false);
    } else if (message.type === 'done') {
      setBusy(false);
      hideProgress();
      appendLog('Analysis complete.');
    }
  });

  // --- Zoom controls ---
  document.getElementById('zoom-in').addEventListener('click', function () {
    if (_zoom && _svg) { _svg.transition().duration(200).call(_zoom.scaleBy, 1.3); }
  });
  document.getElementById('zoom-out').addEventListener('click', function () {
    if (_zoom && _svg) { _svg.transition().duration(200).call(_zoom.scaleBy, 1 / 1.3); }
  });

  function zoomToFit(animate) {
    if (!_zoom || !_svg || !_treeBounds) { return; }
    var b = _treeBounds;
    var treeW = b.maxX - b.minX + 100;
    var treeH = b.maxY - b.minY + 100;
    var rect = document.getElementById('tree-svg').getBoundingClientRect();
    var scale = Math.min(rect.width / treeW, rect.height / treeH, 1);
    var cx = (b.minX + b.maxX) / 2;
    var cy = (b.minY + b.maxY) / 2;
    var tx = rect.width / 2 - cx * scale;
    var ty = rect.height / 2 - cy * scale;
    var t = d3.zoomIdentity.translate(tx, ty).scale(scale);
    if (animate) {
      _svg.transition().duration(300).call(_zoom.transform, t);
    } else {
      _svg.call(_zoom.transform, t);
    }
  }

  function zoomToRoot() {
    if (!_zoom || !_svg || !_rootPos) { return; }
    var vw = _rootPos.h * 30;
    var vh = vw;
    var rect = document.getElementById('tree-svg').getBoundingClientRect();
    var scale = Math.min(rect.width / vw, rect.height / vh, 1);
    var tx = rect.width / 2 - _rootPos.x * scale;
    var ty = rect.height / 2 - _rootPos.y * scale;
    var t = d3.zoomIdentity.translate(tx, ty).scale(scale);
    _svg.transition().duration(300).call(_zoom.transform, t);
  }

  function findAncestors(targetPath, node, path) {
    if (node._path === targetPath) { return path.slice(); }
    if (node.children) {
      path.push(node._path);
      for (var i = 0; i < node.children.length; i++) {
        var result = findAncestors(targetPath, node.children[i], path);
        if (result) { return result; }
      }
      path.pop();
    }
    return null;
  }

  function zoomToNode(nodePath) {
    if (!_rootData) { return; }
    var ancestors = findAncestors(nodePath, _rootData, []);
    var needsRerender = false;
    if (ancestors) {
      ancestors.forEach(function (anc) {
        if (collapsedNames.has(anc)) {
          collapsedNames.delete(anc);
          needsRerender = true;
        }
      });
    }
    if (needsRerender) {
      _suppressZoomToFit = true;
      renderTree(_rootData);
      _suppressZoomToFit = false;
    }
    var node = _nodesByPath[nodePath];
    if (!node) { return; }
    var vw = (node._h || 30) * 30;
    var vh = vw;
    var rect = document.getElementById('tree-svg').getBoundingClientRect();
    var scale = Math.min(rect.width / vw, rect.height / vh, 1);
    var tx = rect.width / 2 - node.x * scale;
    var ty = rect.height / 2 - node.y * scale;
    var t = d3.zoomIdentity.translate(tx, ty).scale(scale);
    _svg.transition().duration(300).call(_zoom.transform, t);
  }

  function isInitialCollapseState() {
    if (collapsedNames.size !== _initialCollapsedNames.size) { return false; }
    for (var name of collapsedNames) {
      if (!_initialCollapsedNames.has(name)) { return false; }
    }
    return true;
  }

  function resetCollapse() {
    if (!_rootData) { return; }
    collapsedNames.clear();
    autoCollapseForLargeModel(_rootData);
    renderTree(_rootData);
    zoomToFit(true);
  }

  // --- Auto-collapse for large models ---
  function autoCollapseForLargeModel(root) {
    var total = 0;
    (function count(n) { total++; if (n.children) { n.children.forEach(count); } })(root);
    if (total <= 150) { return; }

    _isLargeModel = true;
    var currentLevel = [root];
    var cumulative = 1;

    while (currentLevel.length > 0) {
      var nextLevel = [];
      currentLevel.forEach(function (node) {
        if (node.children) {
          node.children.forEach(function (child) { nextLevel.push(child); });
        }
      });
      if (nextLevel.length === 0) { break; }
      if (cumulative + nextLevel.length > 150) {
        currentLevel.forEach(function (node) {
          if (node.children && node.children.length > 0) {
            collapsedNames.add(node._path);
          }
        });
        (function collapseDeep(nodes) {
          nodes.forEach(function (n) {
            if (n.children && n.children.length > 0) {
              collapsedNames.add(n._path);
              collapseDeep(n.children);
            }
          });
        })(nextLevel);
        break;
      }
      cumulative += nextLevel.length;
      currentLevel = nextLevel;
    }
  }

  // --- Main render entry ---
  function render(data) {
    if (!data) { return; }
    _rootData = data;

    // Build unique paths for each node
    buildPathMap(data, '');

    // Reset collapse state
    collapsedNames.clear();
    _isLargeModel = false;
    autoCollapseForLargeModel(data);
    _initialCollapsedNames = new Set(collapsedNames);

    renderTree(data);
    renderStats(data);
  }

  // Deep-clone pruning collapsed children
  function pruneCollapsed(node) {
    var clone = {
      name: node.name, type: node.type, _path: node._path,
      id: node.id || '', content: node.content != null ? node.content : null,
      summary: node.summary || '',
      investigation: node.investigation || '',
      file: node.file || '', lineStart: node.lineStart || -1, lineEnd: node.lineEnd || -1,
      colStart: node.colStart != null ? node.colStart : -1,
      colEnd: node.colEnd != null ? node.colEnd : -1,
      pcCondition: node.pcCondition || '',
      pcColor: node.pcColor || '',
      pcDepth: node.pcDepth != null ? node.pcDepth : -1,
      _alt_type: node._alt_type || ''
    };
    if (collapsedNames.has(node._path) && node.children && node.children.length > 0) {
      clone._collapsed = true;
      clone._childCount = countDescendants(node);
      clone.children = [];
    } else if (node.children) {
      clone.children = node.children.map(function (c) { return pruneCollapsed(c); });
    } else {
      clone.children = [];
    }
    return clone;
  }

  function countDescendants(node) {
    var count = 0;
    if (node.children) {
      node.children.forEach(function (c) {
        count += 1 + countDescendants(c);
      });
    }
    return count;
  }

  function preMeasure(hierarchy) {
    var svg = d3.select('#tree-svg');
    var measureG = svg.append('g').attr('class', 'measure-group').style('visibility', 'hidden');

    hierarchy.each(function (d) {
      var text = measureG.append('text')
        .text(d.data.name)
        .style('font-size', '12px');
      var bbox = text.node().getBBox();
      var extra = d.depth === 0 ? ROOT_EXTRA_PAD : 0;
      d._w = bbox.width + PAD_X * 2 + extra * 2;
      d._h = bbox.height + PAD_Y * 2 + extra * 2;
    });

    measureG.remove();
  }

  function renderTree(rootData) {
    var svg = d3.select('#tree-svg');
    svg.selectAll('*').remove();

    var prunedData = pruneCollapsed(rootData);
    var hierarchy = d3.hierarchy(prunedData, function (d) { return d.children; });

    preMeasure(hierarchy);

    var treeLayout = d3.tree()
      .nodeSize([1, 300])
      .separation(function (a, b) {
        var aHalf = (a._w || 100) / 2;
        var bHalf = (b._w || 100) / 2;
        return aHalf + bHalf + 40;
      });

    treeLayout(hierarchy);

    var container = svg.append('g');

    var zoom = d3.zoom()
      .scaleExtent([0.02, 4])
      .filter(function (event) {
        // Allow zoom for scroll/wheel and left-click drag only; block right-click
        return event.type === 'wheel' || (event.type !== 'contextmenu' && event.button === 0);
      })
      .on('zoom', function (event) {
        container.attr('transform', event.transform);
      });

    svg.call(zoom).on('dblclick.zoom', null);
    _zoom = zoom;
    _svg = svg;

    var nodes = hierarchy.descendants();
    var minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
    nodes.forEach(function (n) {
      var hw = (n._w || 100) / 2;
      var hh = (n._h || 30) / 2;
      if (n.x - hw < minX) { minX = n.x - hw; }
      if (n.x + hw > maxX) { maxX = n.x + hw; }
      if (n.y - hh < minY) { minY = n.y - hh; }
      if (n.y + hh > maxY) { maxY = n.y + hh; }
    });

    _treeBounds = { minX: minX, maxX: maxX, minY: minY, maxY: maxY };
    var rootNode = nodes[0];
    _rootPos = { x: rootNode.x, y: rootNode.y, w: rootNode._w, h: rootNode._h };
    if (!_suppressZoomToFit) {
      zoomToFit(false);
    }

    // Layers
    var linkLayer = container.append('g').attr('class', 'layer-links');
    var nodeLayer = container.append('g').attr('class', 'layer-nodes');

    // Draw links
    var links = hierarchy.links();
    linkLayer.selectAll('.link')
      .data(links)
      .enter()
      .append('path')
      .attr('class', function (d) {
        return d.source.data.type === 'PC' ? 'link link-pc' : 'link';
      })
      .attr('d', function (d) {
        var sx = d.source.x;
        var sy = d.source.y + d.source._h / 2;
        var tx = d.target.x;
        var ty = d.target.y - d.target._h / 2;
        var my = (sy + ty) / 2;
        return 'M' + sx + ',' + sy + ' C' + sx + ',' + my + ' ' + tx + ',' + my + ' ' + tx + ',' + ty;
      });

    // Add labels to PC links
    linkLayer.selectAll('.link-pc-label')
      .data(links.filter(function (d) { return d.source.data.type === 'PC'; }))
      .enter()
      .append('text')
      .attr('class', 'link-pc-label')
      .attr('x', function (d) { return (d.source.x + d.target.x) / 2; })
      .attr('y', function (d) {
        var sy = d.source.y + d.source._h / 2;
        var ty = d.target.y - d.target._h / 2;
        return (sy + ty) / 2 - 4;
      })
      .attr('text-anchor', 'middle')
      .text(function (d) {
        var child = d.target.data;
        var altType = child._alt_type || child.type || '';
        var cond = child.pcCondition || '';
        if (altType === 'if_pc' || altType === 'elif_pc') {
          return altType.replace('_pc', '') + ' ' + cond;
        } else if (altType === 'else_pc') {
          return 'else';
        }
        // For collapsed single-child: check name prefix
        var name = child.name || '';
        if (name.indexOf('if_pc') === 0) { return name.split(':')[0].replace('_pc', ''); }
        if (name.indexOf('elif_pc') === 0) { return name.split(':')[0].replace('_pc', ''); }
        if (name.indexOf('else_pc') === 0) { return 'else'; }
        return cond ? 'if ' + cond : '';
      });

    // Draw nodes
    var nodeGroup = nodeLayer.selectAll('.node')
      .data(nodes)
      .enter()
      .append('g')
      .attr('class', function (d) {
        var cls = 'node';
        if (d.depth === 0) { cls += ' node-root'; }
        if (d.data._collapsed) { cls += ' node-collapsed'; }
        var colorClass = getNodeColorClass(d.data.type);
        if (colorClass) { cls += ' ' + colorClass; }
        // Nodes with children get gray border (unless overridden by collapsed/scope/etc.)
        if (d.data.children && d.data.children.length > 0) { cls += ' node-has-children'; }
        if (isCodeNode(d)) { cls += ' node-clickable'; }
        return cls;
      })
      .attr('transform', function (d) { return 'translate(' + d.x + ',' + d.y + ')'; });

    nodeGroup.each(function (d) {
      var g = d3.select(this);
      var w = d._w;
      var h = d._h;

      var rect = g.append('rect')
        .attr('class', 'node-rect')
        .attr('x', -w / 2)
        .attr('y', -h / 2)
        .attr('width', w)
        .attr('height', h);

      // Apply dynamic PC color if present
      if (d.data.pcColor) {
        rect.style('stroke', d.data.pcColor);
        rect.style('stroke-width', d.data.type === 'PC' ? '3' : '2');
        // Stronger fill for deeper nesting: depth 0=20%, 1=40%, 2=60%, etc.
        var depth = d.data.pcDepth >= 0 ? d.data.pcDepth : 0;
        var opacity = Math.min(0.12 + depth * 0.15, 0.6);
        var opHex = Math.round(opacity * 255).toString(16).padStart(2, '0');
        rect.style('fill', d.data.pcColor + opHex);
      }

      g.append('text')
        .text(d.data.name)
        .attr('y', 0);

      // Collapsed indicator
      if (d.data._collapsed) {
        var ig = g.append('g')
          .attr('class', 'collapse-indicator')
          .attr('transform', 'translate(0,' + (h / 2 + 1) + ')');
        ig.append('rect')
          .attr('x', -10)
          .attr('y', 0)
          .attr('width', 20)
          .attr('height', 14);
        ig.append('text')
          .text('+' + d.data._childCount)
          .attr('x', 0)
          .attr('y', 7);
      }
    });

    // Context menu
    setupContextMenu(nodeGroup);

    // Build path lookup for search/zoom
    _nodesByPath = {};
    _nodesById = {};
    nodes.forEach(function (n) {
      _nodesByPath[n.data._path] = n;
      if (n.data.id) { _nodesById[n.data.id] = n; }
    });

    // Redraw edge arrows if active
    if (_activeEdgeChunkId) { drawEdgeArrows(_activeEdgeChunkId); }
  }

  // --- Context menu ---
  document.addEventListener('click', function () {
    document.getElementById('context-menu').style.display = 'none';
  });

  function showContextMenu(event, items) {
    var menu = document.getElementById('context-menu');
    menu.innerHTML = '';
    items.forEach(function (item) {
      var div = document.createElement('div');
      div.className = 'context-menu-item';
      div.textContent = item.label;
      div.addEventListener('click', function () {
        menu.style.display = 'none';
        item.action();
      });
      menu.appendChild(div);
    });
    menu.style.display = 'block';
    menu.style.left = event.clientX + 'px';
    menu.style.top = event.clientY + 'px';
  }

  // Check if a D3 hierarchy node is below a FILE node (i.e., is a code node)
  function isCodeNode(d) {
    var ancestor = d.parent;
    while (ancestor) {
      if (ancestor.data.type === 'FILE') { return true; }
      ancestor = ancestor.parent;
    }
    return false;
  }

  function hasOriginalChildren(path, node) {
    if (node._path === path) {
      return node.children && node.children.length > 0;
    }
    if (node.children) {
      for (var i = 0; i < node.children.length; i++) {
        var result = hasOriginalChildren(path, node.children[i]);
        if (result !== undefined) { return result; }
      }
    }
    return undefined;
  }

  function setupContextMenu(nodeGroup) {
    nodeGroup.on('contextmenu', function (event, d) {
      event.preventDefault();
      event.stopPropagation();

      var items = [];
      var hasChildren = hasOriginalChildren(d.data._path, _rootData);
      if (hasChildren) {
        var isCollapsed = collapsedNames.has(d.data._path);
        items.push({
          label: isCollapsed ? 'Expand' : 'Collapse',
          action: function () {
            if (isCollapsed) {
              collapsedNames.delete(d.data._path);
            } else {
              collapsedNames.add(d.data._path);
            }
            _suppressZoomToFit = true;
            renderTree(_rootData);
            _suppressZoomToFit = false;
            zoomToNode(d.data._path);
          }
        });
        if (!isCollapsed) {
          items.push({
            label: 'Collapse all children',
            action: function () {
              collapseAllDescendants(d.data._path, _rootData);
              _suppressZoomToFit = true;
              renderTree(_rootData);
              _suppressZoomToFit = false;
              zoomToNode(d.data._path);
            }
          });
        }
      }

      // Show code option — available on all nodes (we'll resolve the code at click time)
      items.push({
        label: 'Show code',
        action: function () {
          showCodeForNode(d.data, d);
        }
      });

      // Show summary option — only for leaf nodes with summaries
      if (d.data.summary && (!d.data.children || d.data.children.length === 0)) {
        items.push({
          label: 'Show summary',
          action: function () {
            showSummaryForNode(d.data);
          }
        });
      }

      // Show edges option
      if (d.data.id) {
        items.push({
          label: 'Show edges',
          action: function () {
            _activeEdgeChunkId = d.data.id;
            drawEdgeArrows(d.data.id);
            showEdgeDetailsInCodeTab(d.data.id);
          }
        });
      }

      // Investigate impact — only for leaf nodes when Neo4j is running
      if (d.data.id && _neo4jRunning && (!d.data.children || d.data.children.length === 0) && !_isBusy) {
        items.push({
          label: 'Investigate impact',
          action: function () {
            setBusy(true);
            vscode.postMessage({ type: 'investigate', nodeId: d.data.id });
            appendLog('Starting impact investigation for ' + d.data.name + ' (' + d.data.id + ')...');
          }
        });
      }

      // Show investigation — when a previous investigation exists for this leaf
      if (d.data.investigation && (!d.data.children || d.data.children.length === 0)) {
        items.push({
          label: 'Show investigation',
          action: function () {
            showInvestigationResult(d.data.id, d.data.investigation);
          }
        });
      }

      // Dump node subtree to file
      if (d.data.id) {
        items.push({
          label: 'Dump node to file',
          action: function () {
            vscode.postMessage({ type: 'dumpNode', nodeId: d.data.id });
            appendLog('Dumping node ' + d.data.id + ' to file...');
          }
        });
      }

      items.push({ label: 'Zoom to root', action: zoomToRoot });
      items.push({ label: 'Zoom to fit', action: function () { zoomToFit(true); } });
      if (_isLargeModel && !isInitialCollapseState()) {
        items.push({ label: 'Reset collapse', action: resetCollapse });
      }

      showContextMenu(event, items);
    });

    _svg.on('contextmenu', function (event) {
      if (event.target.closest('.node')) { return; }
      event.preventDefault();
      var bgItems = [
        { label: 'Zoom to root', action: zoomToRoot },
        { label: 'Zoom to fit', action: function () { zoomToFit(true); } }
      ];
      if (_activeEdgeChunkId) {
        bgItems.push({
          label: 'Clear edges',
          action: function () {
            _activeEdgeChunkId = null;
            d3.select('#tree-svg').selectAll('.layer-edge-arrows').remove();
            d3.select('#tree-svg').selectAll('.layer-embedding-bg').remove();
          }
        });
      }
      if (_isLargeModel && !isInitialCollapseState()) {
        bgItems.push({ label: 'Reset collapse', action: resetCollapse });
      }
      showContextMenu(event, bgItems);
    });
  }

  function collapseAllDescendants(path, node) {
    if (node._path === path) {
      if (node.children) {
        node.children.forEach(function (child) {
          if (child.children && child.children.length > 0) {
            collapsedNames.add(child._path);
            collapseAllDescendantsRecursive(child);
          }
        });
      }
      return true;
    }
    if (node.children) {
      for (var i = 0; i < node.children.length; i++) {
        if (collapseAllDescendants(path, node.children[i])) { return true; }
      }
    }
    return false;
  }

  function collapseAllDescendantsRecursive(node) {
    if (node.children) {
      node.children.forEach(function (child) {
        if (child.children && child.children.length > 0) {
          collapsedNames.add(child._path);
          collapseAllDescendantsRecursive(child);
        }
      });
    }
  }

  // --- Edge arrows ---
  function getEdgesForChunk(chunkId) {
    // Return all edges where this chunk is src or dst
    var result = [];
    for (var i = 0; i < _allEdges.length; i++) {
      var e = _allEdges[i];
      if (e.srcId === chunkId || e.dstId === chunkId) {
        result.push(e);
      }
    }
    return result;
  }

  function drawEdgeArrows(chunkId) {
    // Remove old edge layers
    d3.select('#tree-svg').selectAll('.layer-edge-arrows').remove();
    d3.select('#tree-svg').selectAll('.layer-embedding-bg').remove();

    var edges = getEdgesForChunk(chunkId);
    if (edges.length === 0) { return; }

    var container = d3.select('#tree-svg').select('g');
    if (container.empty()) { return; }

    var svg = d3.select('#tree-svg');
    var defs = svg.select('defs');
    if (defs.empty()) { defs = svg.insert('defs', ':first-child'); }
    defs.selectAll('#edge-arrowhead').remove();
    defs.selectAll('#provides-arrowhead').remove();
    defs.append('marker')
      .attr('id', 'edge-arrowhead')
      .attr('viewBox', '0 0 10 10')
      .attr('refX', 10).attr('refY', 5)
      .attr('markerWidth', 8).attr('markerHeight', 8)
      .attr('orient', 'auto')
      .append('path')
      .attr('d', 'M 0 0 L 10 5 L 0 10 Z')
      .attr('fill', '#e05252');
    defs.append('marker')
      .attr('id', 'provides-arrowhead')
      .attr('viewBox', '0 0 10 10')
      .attr('refX', 10).attr('refY', 5)
      .attr('markerWidth', 8).attr('markerHeight', 8)
      .attr('orient', 'auto')
      .append('path')
      .attr('d', 'M 0 0 L 10 5 L 0 10 Z')
      .attr('fill', '#2e8b57');

    // Embedding edges go behind everything, structural edges on top
    var embeddingLayer = container.insert('g', ':first-child').attr('class', 'layer-embedding-bg');
    var arrowLayer = container.append('g').attr('class', 'layer-edge-arrows');

    edges.forEach(function (e) {
      var srcNode = _nodesById[e.srcId];
      var dstNode = _nodesById[e.dstId];
      if (!srcNode || !dstNode || srcNode === dstNode) { return; }

      var isEmbedding = e.edgeType === 'EMBEDDING';
      var isProvides = e.edgeType === 'PROVIDES';
      var isAnalysis = isEmbedding || isProvides;
      var sw = (srcNode._w || 100) / 2, sh = (srcNode._h || 30) / 2;
      var tw = (dstNode._w || 100) / 2, th = (dstNode._h || 30) / 2;

      var sx, sy, tx, ty;
      if (isAnalysis) {
        // Embedding and PROVIDES edges connect at bottom of nodes
        sx = srcNode.x;  sy = srcNode.y + sh;
        tx = dstNode.x;  ty = dstNode.y + th;
      } else {
        // Structural edges: left/right or top/bottom
        var hdist = dstNode.x - srcNode.x;
        if (Math.abs(hdist) > 1) {
          if (hdist > 0) { sx = srcNode.x + sw; sy = srcNode.y; tx = dstNode.x - tw; ty = dstNode.y; }
          else { sx = srcNode.x - sw; sy = srcNode.y; tx = dstNode.x + tw; ty = dstNode.y; }
        } else {
          if (dstNode.y > srcNode.y) { sx = srcNode.x; sy = srcNode.y + sh; tx = dstNode.x; ty = dstNode.y - th; }
          else { sx = srcNode.x; sy = srcNode.y - sh; tx = dstNode.x; ty = dstNode.y + th; }
        }
      }

      var mx = (sx + tx) / 2, my = (sy + ty) / 2;
      var dx = tx - sx, dy = ty - sy;
      var dist = Math.sqrt(dx * dx + dy * dy) || 1;
      var offset = Math.min(40, dist * 0.15);
      var nx = -dy / dist * offset, ny = dx / dist * offset;

      if (isEmbedding) {
        embeddingLayer.append('path')
          .attr('class', 'edge-arrow-similarity')
          .attr('d', 'M' + sx + ',' + sy + ' Q' + (mx + nx) + ',' + (my + ny) + ' ' + tx + ',' + ty);
      } else if (isProvides) {
        embeddingLayer.append('path')
          .attr('class', 'edge-arrow-provides')
          .attr('d', 'M' + sx + ',' + sy + ' Q' + (mx + nx) + ',' + (my + ny) + ' ' + tx + ',' + ty)
          .attr('marker-end', 'url(#provides-arrowhead)');
      } else {
        arrowLayer.append('path')
          .attr('class', 'edge-arrow')
          .attr('d', 'M' + sx + ',' + sy + ' Q' + (mx + nx) + ',' + (my + ny) + ' ' + tx + ',' + ty)
          .attr('marker-end', 'url(#edge-arrowhead)');
      }
    });
  }

  function showEdgeDetailsInCodeTab(chunkId) {
    var edges = getEdgesForChunk(chunkId);

    var headerDiv = document.createElement('div');
    headerDiv.className = 'code-header';
    headerDiv.textContent = 'Edges for chunk ' + chunkId + '  \u2014  ' + edges.length + ' edge(s)';

    var bodyDiv = document.createElement('div');
    bodyDiv.className = 'code-body';

    if (edges.length === 0) {
      bodyDiv.innerHTML = '<span class="code-placeholder">No edges for this node.</span>';
    } else {
      var parts = [];
      for (var i = 0; i < edges.length; i++) {
        var e = edges[i];
        var isOutgoing = e.srcId === chunkId;
        var arrow = isOutgoing ? '\u2192' : '\u2190';
        var otherId = isOutgoing ? e.dstId : e.srcId;
        var otherName = isOutgoing ? e.dstName : e.srcName;
        var extraLabel = '';
        if (e.similarity) { extraLabel += '  (sim: ' + e.similarity.toFixed(4) + ')'; }
        if (e.symbol) { extraLabel += '  [symbol: ' + e.symbol + ']'; }
        parts.push(arrow + ' ' + e.edgeType + '  ' + otherName + '  [' + otherId + ']' + extraLabel);
        var srcLoc = (e.srcFile || '') + ':' + e.srcLine;
        var dstLoc = (e.dstFile || '') + ':' + e.dstLine;
        parts.push('    src: ' + srcLoc + '  ' + escapeHtml(e.srcName));
        parts.push('    dst: ' + dstLoc + '  ' + escapeHtml(e.dstName));
        if (i < edges.length - 1) {
          parts.push('────────────────────────────────────────');
        }
        parts.push('');
      }
      bodyDiv.textContent = parts.join('\n');
    }

    codeContent.innerHTML = '';
    codeContent.appendChild(headerDiv);
    codeContent.appendChild(bodyDiv);

    // Switch to code tab
    document.querySelectorAll('.panel-tab').forEach(function (t) { t.classList.remove('active'); });
    document.querySelectorAll('.tab-content').forEach(function (tc) { tc.classList.remove('active'); });
    document.querySelector('.panel-tab[data-tab="code"]').classList.add('active');
    document.getElementById('tab-code').classList.add('active');
    if (panelCollapsed) {
      panelCollapsed = false;
      document.querySelectorAll('.tab-content').forEach(function (tc) { tc.style.display = ''; });
      document.getElementById('panel-toggle').textContent = '\u25BC';
    }
  }

  // --- Stats ---
  function renderStats(data) {
    var totalNodes = 0;
    var maxDepth = 0;
    var typeCounts = {};
    var pcLeaves = 0;
    var nonPcLeaves = 0;
    var pcCount = 0;

    (function walk(node, depth, inPc) {
      totalNodes++;
      if (depth > maxDepth) { maxDepth = depth; }
      var t = node.type || 'UNKNOWN';
      typeCounts[t] = (typeCounts[t] || 0) + 1;
      if (t === 'PC') { pcCount++; inPc = true; }
      var isLeaf = !node.children || node.children.length === 0;
      if (isLeaf) {
        if (inPc) { pcLeaves++; } else { nonPcLeaves++; }
      }
      if (node.children) {
        node.children.forEach(function (c) { walk(c, depth + 1, inPc); });
      }
    })(data, 0, false);

    var overlay = document.getElementById('stats-overlay');
    var html = '<strong>AST Stats</strong><br>' +
      'Total lines of code: ' + _totalLoc + '<br>' +
      'Total nodes: ' + totalNodes + '<br>' +
      'Max depth: ' + maxDepth + '<br>' +
      '<br><strong>Presence conditions:</strong><br>' +
      'PC nodes: ' + pcCount + '<br>' +
      'Leaves in PCs: ' + pcLeaves + '<br>' +
      'Leaves outside PCs: ' + nonPcLeaves + '<br>';

    // Show top types
    var sortedTypes = Object.keys(typeCounts).sort(function (a, b) {
      return typeCounts[b] - typeCounts[a];
    });
    html += '<br><strong>Node types:</strong><br>';
    sortedTypes.slice(0, 10).forEach(function (t) {
      html += t + ': ' + typeCounts[t] + '<br>';
    });

    overlay.innerHTML = html;
  }

  // --- PNG export ---
  document.getElementById('export-png').addEventListener('click', exportPng);

  function exportPng() {
    if (!_treeBounds || !_svg) { return; }
    var padding = 40;
    var b = _treeBounds;
    var vbX = b.minX - padding;
    var vbY = b.minY - padding;
    var vbW = (b.maxX - b.minX) + padding * 2;
    var vbH = (b.maxY - b.minY) + padding * 2;

    var svgEl = document.getElementById('tree-svg');
    var clone = svgEl.cloneNode(true);
    clone.setAttribute('width', vbW);
    clone.setAttribute('height', vbH);
    clone.setAttribute('viewBox', vbX + ' ' + vbY + ' ' + vbW + ' ' + vbH);

    var containerG = clone.querySelector('g');
    if (containerG) { containerG.removeAttribute('transform'); }

    inlineStylesFromSource(svgEl, clone);

    var serializer = new XMLSerializer();
    var svgString = serializer.serializeToString(clone);
    var blob = new Blob([svgString], { type: 'image/svg+xml;charset=utf-8' });
    var url = URL.createObjectURL(blob);

    var scale = 2;
    var img = new Image();
    img.onload = function () {
      var canvas = document.createElement('canvas');
      canvas.width = vbW * scale;
      canvas.height = vbH * scale;
      var ctx = canvas.getContext('2d');
      var bg = getComputedStyle(document.body).backgroundColor || '#1e1e1e';
      ctx.fillStyle = bg;
      ctx.fillRect(0, 0, canvas.width, canvas.height);
      ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
      URL.revokeObjectURL(url);
      var dataUrl = canvas.toDataURL('image/png');
      vscode.postMessage({ type: 'exportPng', dataUrl: dataUrl });
    };
    img.src = url;
  }

  function inlineStylesFromSource(source, clone) {
    if (source.nodeType !== 1) { return; }
    var computed = getComputedStyle(source);
    var props = ['fill', 'stroke', 'stroke-width', 'stroke-dasharray', 'opacity',
      'font-size', 'font-family', 'font-weight', 'text-anchor', 'dominant-baseline',
      'color', 'rx', 'ry'];
    props.forEach(function (prop) {
      var val = computed.getPropertyValue(prop);
      if (val) { clone.style.setProperty(prop, val); }
    });
    var sc = source.children;
    var cc = clone.children;
    var len = Math.min(sc.length, cc.length);
    for (var i = 0; i < len; i++) {
      inlineStylesFromSource(sc[i], cc[i]);
    }
  }
})();
