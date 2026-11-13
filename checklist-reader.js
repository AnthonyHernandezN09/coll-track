(function () {
  const headingSelector = 'h1,h2,h3,h4,h5,h6,[role="heading"]';
  const tableSelector = 'table,[role="table"],[role="grid"],[role="treegrid"]';
  const rowSelector = '[role="row"]';
  const cellSelector = '[role="cell"],[role="gridcell"],[role="columnheader"],[role="rowheader"]';
  const statusLabels = new Map([
    ['received', 'received'], ['complete', 'complete'], ['completed', 'complete'],
    ['awaiting', 'pending'], ['awaiting materials', 'pending'], ['pending', 'pending'],
    ['missing', 'missing'], ['not received', 'missing'], ['waived', 'waived'],
    ['not required', 'not_required']
  ]);
  const clean = value => (value ?? '').replace(/\s+/g, ' ').trim();
  const label = value => clean(value).toLowerCase().replace(/:$/, '');
  function isVisible(element, ignoreAriaHidden) {
    const view = element.ownerDocument.defaultView;
    for (let node = element; node; node = node.parentElement) {
      if (node.hidden || (!ignoreAriaHidden && node.getAttribute('aria-hidden') === 'true')) return false;
      const style = view?.getComputedStyle(node);
      if (style?.display === 'none' || style?.visibility === 'hidden') return false;
    }
    return true;
  }
  const visible = element => isVisible(element, false);

  function checklistKind(title) {
    const name = label(title);
    if (/^(?:(?:your )?enrollment|admitted student) checklist$/.test(name)) return 'enrollment';
    if (/^forms? checklist$/.test(name)) return 'forms';
    if (/^(?:(?:your )?(?:application|admissions?|materials?|documents?|requirements?) )?checklist$/.test(name)) {
      return 'admissions';
    }
    if (/^(?:required (?:documents|materials)|application requirements)$/.test(name)) return 'admissions';
    return null;
  }

  function columnRole(value) {
    const name = label(value);
    if (/^(?:status|item status|requirement status|document status|completion status)$/.test(name)) return 'status';
    if (/^(?:items?|details?|requirements?|materials?|documents?|forms?|checklist item|(?:item|requirement|document|material|form) name)$/.test(name)) return 'item';
    if (/^(?:dates?|status date|date received|date complete|date completed|received date|completed date|due date|deadline)$/.test(name)) return 'date';
    return null;
  }

  const display = element => element.ownerDocument.defaultView?.getComputedStyle(element).display;
  const isGrid = element => ['grid', 'inline-grid'].includes(display(element));

  function rowGroups(table) {
    if (table.tagName === 'TABLE') {
      const rows = Array.from(table.rows).filter(visible);
      return {
        rows,
        candidates: table.tHead ? rows.filter(row => row.parentElement === table.tHead) : rows.slice(0, 1),
        cellsOf: row => Array.from(row.cells)
      };
    }

    if (table.matches(tableSelector)) {
      const rows = Array.from(table.querySelectorAll(rowSelector))
        .filter(row => row.closest(tableSelector) === table && visible(row));
      const cellsOf = row => Array.from(row.querySelectorAll(cellSelector))
        .filter(cell => cell.closest(rowSelector) === row && cell.closest(tableSelector) === table);
      const headers = rows.filter(row => cellsOf(row).some(cell => cell.matches('[role="columnheader"]')));
      return { rows, candidates: headers.length ? headers : rows.slice(0, 1), cellsOf };
    }

    const rows = Array.from(table.children).filter(visible);
    if (rows.length < 2 || !rows.every(row => row.children.length >= 2 &&
      (isGrid(row) || (isGrid(table) && display(row) === 'contents')))) return null;
    return { rows, candidates: rows.slice(0, 1), cellsOf: row => Array.from(row.children) };
  }

  const colSpan = cell => cell.colSpan ?? Number(cell.getAttribute('aria-colspan') ?? 1);
  const rowSpan = cell => cell.rowSpan ?? Number(cell.getAttribute('aria-rowspan') ?? 1);

  function tableSchema(table, groups) {
    if (!groups) return null;
    const { rows, candidates, cellsOf } = groups;
    const schemas = candidates.flatMap(row => {
      const cells = cellsOf(row);
      const labels = cells.map(cell => clean(cell.innerText ?? cell.textContent));
      const roles = labels.map(columnRole);
      const details = labels.map(value => /^details?$/.test(label(value)));
      if (roles.filter((role, index) => role === 'item' && !details[index]).length === 1) {
        for (const [index, isDetails] of details.entries()) {
          if (isDetails) roles[index] = null;
        }
      }
      const widths = cells.map(colSpan);
      if (cells.some((cell, index) =>
        rowSpan(cell) !== 1 ||
        (!visible(cell) && (roles[index] === 'status' || roles[index] === 'item')) ||
        (widths[index] !== 1 && !(roles[index] === 'status' && widths[index] === 2))
      )) return [];
      if (['status', 'item'].some(role => roles.filter(value => value === role).length !== 1)) return [];
      if (roles.filter(role => role === 'date').length > 1) return [];
      return [{ row, labels, roles, widths }];
    });
    if (schemas.length !== 1) return null;

    const { row, labels, roles, widths } = schemas[0];
    return {
      labels,
      roles,
      widths,
      rows: rows.slice(rows.indexOf(row) + 1).filter(candidate =>
        candidate.parentElement !== table.tHead && candidate.parentElement !== table.tFoot),
      cellsOf
    };
  }

  function readStatus(cells) {
    const text = [];
    const cues = [];
    for (const cell of cells) {
      const walker = cell.ownerDocument.createTreeWalker(cell, 4);
      while (walker.nextNode()) {
        const node = walker.currentNode;
        if (visible(node.parentElement)) text.push(node.textContent);
      }
      for (const node of [cell, ...cell.querySelectorAll('[alt],[aria-label],[title]')]) {
        for (const attribute of ['alt', 'aria-label', 'title']) {
          const cue = clean(node.getAttribute(attribute));
          // Slate can hide the icon cell while retaining its explicit status label.
          if (cue && (visible(node) || (cells.length === 2 && /^status\s*:/i.test(cue)))) {
            cues.push(cue);
          }
        }
      }
    }
    const rawText = clean(text.join(' '));
    const known = new Map();
    for (const raw of [rawText, ...cues]) {
      const status = statusLabels.get(label(raw).replace(/^status\s*:\s*/, ''));
      if (status) known.set(status, raw);
    }
    if (!rawText && !cues.length && cells.some(cell => visible(cell) &&
      Array.from(cell.querySelectorAll('svg.bi-check-lg')).some(icon => isVisible(icon, true)))) {
      known.set('complete', 'Complete');
    }
    return {
      status: known.size === 1 ? known.keys().next().value : 'unknown',
      rawStatus: rawText || (known.size === 1 ? known.values().next().value : '') || null
    };
  }

  function readChecklists(root = document) {
    const checklists = [];
    const unmatched = new Map();
    const inferred = [];
    const headings = new Map();
    const candidates = headingSelector + ',' + tableSelector + ',div,section,ul';
    for (const element of root.querySelectorAll(candidates)) {
      const scope = element.parentElement?.closest(tableSelector) ?? root;
      if (element.matches(headingSelector)) {
        if (visible(element)) headings.set(scope, clean(element.innerText ?? element.textContent));
        continue;
      }

      const caption = element.caption && visible(element.caption)
        ? clean(element.caption.innerText ?? element.caption.textContent) : '';
      const names = (element.getAttribute('aria-labelledby') ?? '').split(/\s+/).filter(Boolean);
      const explicitTitle = caption || element.getAttribute('aria-label') ||
        names.map(id => element.ownerDocument.getElementById(id)?.textContent ?? '').join(' ');
      const heading = headings.get(scope);
      const title = checklistKind(explicitTitle) ? explicitTitle : heading;
      const kind = checklistKind(title);
      if (!visible(element) || (!kind && !element.matches(tableSelector))) continue;

      const semantic = element.matches(tableSelector);
      const groups = rowGroups(element);
      const schema = tableSchema(element, groups);
      if (!schema) {
        if (kind && semantic && !element.querySelector(tableSelector)) {
          const hasStatus = groups?.candidates.some(row =>
            groups.cellsOf(row).some(cell => columnRole(cell.innerText ?? cell.textContent) === 'status'));
          if (hasStatus) {
            unmatched.set(scope, title);
          }
        }
        continue;
      }

      const statusColumn = schema.roles.indexOf('status');
      const itemColumn = schema.roles.indexOf('item');
      const dateColumn = schema.roles.indexOf('date');
      const offset = column => schema.widths.slice(0, column).reduce((sum, width) => sum + width, 0);
      const statusIndex = offset(statusColumn);
      const statusWidth = schema.widths[statusColumn];
      const itemIndex = offset(itemColumn);
      const dateIndex = dateColumn < 0 ? -1 : offset(dateColumn);
      const width = offset(schema.widths.length);
      const items = [];
      let skippedRows = 0;
      for (const row of schema.rows) {
        const cells = schema.cellsOf(row);
        if (cells.length !== width ||
            cells.some(cell => colSpan(cell) !== 1 || rowSpan(cell) !== 1) ||
            !visible(cells[itemIndex])) {
          skippedRows++;
          continue;
        }
        const item = clean(cells[itemIndex].innerText ?? cells[itemIndex].textContent);
        if (!item) {
          skippedRows++;
          continue;
        }
        const date = cells[dateIndex];
        const dateText = date ? clean(date.innerText ?? date.textContent) : '';
        items.push({
          item,
          ...readStatus(cells.slice(statusIndex, statusIndex + statusWidth)),
          dateText: dateText && label(dateText) !== 'none' ? dateText : null
        });
      }
      if (!kind) {
        const genericItem = /^(?:items?|details?)$/.test(label(schema.labels[itemColumn]));
        const applicationContext = element.ownerDocument.location?.pathname.replace(/\/$/, '') === '/apply/status';
        if (!applicationContext || genericItem || skippedRows || !items.length ||
            !items.every(item => item.status !== 'unknown')) continue;
      } else {
        headings.delete(scope);
      }
      (kind ? checklists : inferred).push({
        title: kind ? title : 'Application Checklist', kind: kind ?? 'admissions',
        dateLabel: schema.labels[dateColumn] ?? null,
        skippedRows,
        items
      });
    }
    const unreadable = [...unmatched.values()].map(title => ({ title, reason: 'unrecognized_columns' }));
    if (!checklists.length && !unreadable.length && inferred.length === 1) checklists.push(inferred[0]);
    return { checklists, unreadable };
  }

  globalThis.CollTrack = Object.freeze({ readChecklists });
})();
