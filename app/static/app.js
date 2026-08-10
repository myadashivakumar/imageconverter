/* Shared, cached helpers used across tool pages. Page-specific logic (crop,
   compress mode toggle, per-tool submit handling) stays inline in each page -
   only the truly identical mechanics live here. */

function formatSize(bytes) {
  if (bytes < 1024) return bytes + ' B';
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
  return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
}

/**
 * Wires a dropzone + hidden file input for click-to-browse and drag-and-drop.
 * Note: drag-and-drop only fires on mouse-driven browsers; the click-to-browse
 * path is what mobile/touch users rely on, so it must always work standalone.
 */
function wireDropzone(dropzone, fileInput, onFiles) {
  dropzone.addEventListener('click', () => fileInput.click());
  fileInput.addEventListener('change', () => {
    onFiles(fileInput.files);
    fileInput.value = '';
  });

  ['dragenter', 'dragover'].forEach((evt) =>
    dropzone.addEventListener(evt, (e) => {
      e.preventDefault();
      dropzone.classList.add('dragover');
    })
  );
  ['dragleave', 'drop'].forEach((evt) =>
    dropzone.addEventListener(evt, (e) => {
      e.preventDefault();
      dropzone.classList.remove('dragover');
    })
  );
  dropzone.addEventListener('drop', (e) => {
    onFiles(e.dataTransfer.files);
  });
}

/**
 * Renders a reorderable, removable list of files with Up/Down move buttons.
 * HTML5 drag-and-drop (dragstart/dragover/drop) never fires on mobile
 * touchscreens, so reordering is done with plain buttons instead - works
 * identically on touch and mouse.
 *
 * @param container   element to render the list into
 * @param files       current array of File objects, in order
 * @param renderIcon  (file, index) => DOM element for the thumbnail/icon slot
 * @param onChange    (newFilesArray) => void, called after reorder/remove
 */
function renderReorderableList(container, files, renderIcon, onChange) {
  container.innerHTML = '';

  files.forEach((file, index) => {
    const item = document.createElement('div');
    item.className = 'preview-item';

    const badge = document.createElement('span');
    badge.className = 'preview-index';
    badge.textContent = index + 1;

    const iconEl = renderIcon(file, index);

    const info = document.createElement('div');
    info.className = 'preview-info';
    const name = document.createElement('div');
    name.className = 'preview-name';
    name.textContent = file.name;
    const size = document.createElement('div');
    size.className = 'preview-size';
    size.textContent = formatSize(file.size);
    info.append(name, size);

    const moveControls = document.createElement('div');
    moveControls.className = 'move-controls';

    const upBtn = document.createElement('button');
    upBtn.type = 'button';
    upBtn.className = 'icon-btn';
    upBtn.setAttribute('aria-label', 'Move ' + file.name + ' up');
    upBtn.disabled = index === 0;
    upBtn.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 15l-6-6-6 6"/></svg>';
    upBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      if (index === 0) return;
      const next = files.slice();
      [next[index - 1], next[index]] = [next[index], next[index - 1]];
      onChange(next);
    });

    const downBtn = document.createElement('button');
    downBtn.type = 'button';
    downBtn.className = 'icon-btn';
    downBtn.setAttribute('aria-label', 'Move ' + file.name + ' down');
    downBtn.disabled = index === files.length - 1;
    downBtn.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M6 9l6 6 6-6"/></svg>';
    downBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      if (index === files.length - 1) return;
      const next = files.slice();
      [next[index], next[index + 1]] = [next[index + 1], next[index]];
      onChange(next);
    });

    moveControls.append(upBtn, downBtn);

    const removeBtn = document.createElement('button');
    removeBtn.type = 'button';
    removeBtn.className = 'icon-btn remove-btn';
    removeBtn.setAttribute('aria-label', 'Remove ' + file.name);
    removeBtn.innerHTML = '&times;';
    removeBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      const next = files.slice();
      next.splice(index, 1);
      onChange(next);
    });

    item.append(badge, iconEl, info, moveControls, removeBtn);
    container.appendChild(item);
  });
}
