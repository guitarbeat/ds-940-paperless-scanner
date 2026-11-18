# macOS Finder Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Transform `https://scans.alw.lol/` into an authentic macOS Finder window experience mirroring the actual directory hierarchy (`/Users/aaron/Documents/brother-scans`) on the Mac Mini, with a Source List sidebar, Icon Grid/List view toggle, and Spacebar-driven Quick Look preview.

**Architecture:** Extend `/api/scans` to emit folder metadata (`folder`, `is_unfiled`) and add `/api/folders` for dynamic filesystem tree introspection. Refactor `serve_dashboard` HTML/CSS/JS into a macOS Finder window containing a toolbar with hardware status, a left Source List sidebar, an icon-grid file browser with path breadcrumbs, a Spacebar-toggled Quick Look inspector, and a quiet statusbar footer.

**Tech Stack:** Python 3 standard library (`http.server`), Vanilla JavaScript (ES6+), CSS Grid/Flexbox with macOS design tokens (SF Pro, Apple easing curves, traffic light controls), Playwright for automated multi-viewport verification.

**Spec:** `spec-finder-redesign.md` (Artifact URI: file:///Users/aaron/.gemini/antigravity/brain/659a0d47-b1ee-45bb-ab44-ce37efc5b2fd/spec-finder-redesign.md)

## Global Constraints
- Target URL: `https://scans.alw.lol/` on remote host `aaron@100.96.177.69`.
- Maintain 100% backward compatibility with all existing unit test selectors in `tests/test_web.py` (`live-status-strip`, `strip-title`, `header-session-actions`, `header-global-actions`, `Recent Scans`, `Doc Date`, `Scanned`).
- All 14 telemetry datapoints (Scanner IP + admin link, Daemon State, Uptime, RSS RAM, Archive path + copy button) must remain permanently visible in the Finder statusbar footer.
- Zero horizontal overflow or element collisions across viewports (1440px Desktop, 1024px Laptop, 768px Tablet, 390px Mobile).
- Git push operations must export `PATH="/opt/homebrew/bin:$PATH"` for macOS Git LFS compatibility.

## Review Focus
1. **Empty folder / zero unfiled state**: When all documents are organized and the inbox has 0 files, the Inbox item displays a clean zero state without layout collapse.
2. **Keyboard focus trap with Spacebar**: Spacebar toggles Quick Look preview only when not actively typing in the search input box.
3. **Long filenames in Icon Grid view**: Truncates cleanly after 2 lines with tooltip showing the complete filename, avoiding grid misalignment.
4. **Mobile viewport (<680px) adaptation**: Sidebar cleanly switches to a top segmented folder bar and Quick Look slides up as a fullscreen modal with close gesture.
5. **Hardware Jam / Error alert coexistence**: Alerts sit right under the window titlebar as a native macOS notification bar without pushing the Finder sidebar out of alignment.

---

### Task 1: Backend Folder Introspection & Metadata API

**Files:**
- Modify: `ds940/web.py` (`get_recent_scans`, `DashboardHandler.do_GET`)
- Modify: `tests/test_web.py`

**Interfaces:**
- Produces: `/api/scans` returning `folder` and `is_unfiled` fields per scan; `/api/folders` returning `[{"name": str, "count": int, "is_root": bool}]`.

- [ ] **Step 1: Write failing test in `tests/test_web.py`**
```python
    def test_folders_and_scan_metadata_endpoint(self):
        # Verify /api/folders endpoint
        url = f"http://127.0.0.1:{self.port}/api/folders"
        with urllib.request.urlopen(url, timeout=3.0) as resp:
            self.assertEqual(resp.status, 200)
            folders = json.loads(resp.read().decode("utf-8"))
            self.assertIsInstance(folders, list)
            names = [f["name"] for f in folders]
            self.assertIn("Inbox", names)
            self.assertIn("Bills", names)

        # Verify /api/scans contains folder and is_unfiled
        scans_url = f"http://127.0.0.1:{self.port}/api/scans"
        with urllib.request.urlopen(scans_url, timeout=3.0) as resp:
            scans = json.loads(resp.read().decode("utf-8"))
            by_name = {s["filename"]: s for s in scans}
            self.assertTrue(by_name["test_doc.pdf"]["is_unfiled"])
            self.assertEqual(by_name["test_doc.pdf"]["folder"], "Inbox")
            self.assertFalse(by_name["2026-10-02_Austin-Energy_Bill.pdf"]["is_unfiled"])
            self.assertEqual(by_name["2026-10-02_Austin-Energy_Bill.pdf"]["folder"], "Bills")
```

- [ ] **Step 2: Run test to verify it fails**
```bash
cd /Users/aaron/Projects/ds-940-paperless-scanner && PYTHONPATH=. /opt/homebrew/bin/python3 -m unittest -v tests/test_web.py
```
Expected: FAIL on `urllib.error.HTTPError: HTTP Error 404: Not Found` for `/api/folders`.

- [ ] **Step 3: Implement minimal code in `ds940/web.py`**
In `get_recent_scans`:
```python
        is_unfiled = path.parent.resolve() == output_dir.resolve()
        folder_name = "Inbox" if is_unfiled else path.parent.name
        scans.append({
            "filename": path.name,
            "relative_path": rel_path,
            "folder": folder_name,
            "is_unfiled": is_unfiled,
            "size_bytes": st.st_size,
            "size_human": format_size(st.st_size),
            "mtime": st.st_mtime,
            "doc_date": doc_date,
            "scan_date": scan_date,
            "category": category,
        })
```
In `DashboardHandler.do_GET`:
```python
        elif path == "/api/folders":
            self.serve_folders()
```
And add `serve_folders(self)`:
```python
    def serve_folders(self):
        output_dir = self.settings.output
        folders = []
        if output_dir.exists():
            root_pdfs = [p for p in output_dir.glob("*.pdf") if p.is_file()]
            folders.append({"name": "Inbox", "count": len(root_pdfs), "is_root": True})
            for sub in sorted(output_dir.iterdir()):
                if sub.is_dir() and not sub.name.startswith("."):
                    sub_pdfs = [p for p in sub.glob("*.pdf") if p.is_file()]
                    folders.append({"name": sub.name, "count": len(sub_pdfs), "is_root": False})
        self.send_json(folders)
```

- [ ] **Step 4: Run test to verify it passes**
```bash
cd /Users/aaron/Projects/ds-940-paperless-scanner && PYTHONPATH=. /opt/homebrew/bin/python3 -m unittest -v tests/test_web.py
```
Expected: PASS (8 tests passed).

- [ ] **Step 5: Commit**
```bash
git add ds940/web.py tests/test_web.py
git commit -m "feat(api): add /api/folders endpoint and folder metadata to scans"
```

---

### Task 2: macOS Finder Window Chrome & Source List Sidebar

**Files:**
- Modify: `ds940/web.py` (`serve_dashboard` styles and markup)
- Test: `tests/test_web.py`

**Interfaces:**
- Produces: Finder window container (`#finder-window`), traffic lights (`.window-traffic-lights`), toolbar status strip (`#live-status-strip`), Source List sidebar (`#finder-sidebar`), breadcrumb path bar (`#finder-breadcrumbs`).

- [ ] **Step 1: Write test assertion in `tests/test_web.py`**
Verify `test_dashboard_html` checks for Finder window and sidebar elements:
```python
            self.assertIn("finder-window", html)
            self.assertIn("finder-sidebar", html)
            self.assertIn("finder-breadcrumbs", html)
```

- [ ] **Step 2: Run test to verify it fails**
```bash
cd /Users/aaron/Projects/ds-940-paperless-scanner && PYTHONPATH=. /opt/homebrew/bin/python3 -m unittest -v tests/test_web.py
```
Expected: FAIL on `assertIn("finder-window", html)`.

- [ ] **Step 3: Implement macOS Finder window structure in `ds940/web.py`**
1. Add Finder window styles:
   - Window shell: `.finder-window` with border radius, subtle border, macOS window drop shadow `0 20px 50px rgba(0,0,0,0.15)`.
   - Titlebar / Toolbar: Traffic lights (`● ● ●`), title (`Brother DS-940DW`), live status beacon, session actions (`+5m`, `Sleep Now`), `⚡ Organize` button, theme toggle, refresh.
   - Left Sidebar (`.finder-sidebar`): macOS Source List with `FAVORITES` header, `📥 Inbox / Unfiled`, and dynamic folders loaded from `/api/folders` with item count badges.
2. In JS, add `loadFolders()` to fetch `/api/folders` and render the sidebar navigation.
3. Keep `#live-status-strip`, `#strip-title`, `#header-session-actions`, `#header-global-actions`, `Recent Scans`, `Doc Date`, and `Scanned` intact so all existing unit tests pass.

- [ ] **Step 4: Run test to verify it passes**
```bash
cd /Users/aaron/Projects/ds-940-paperless-scanner && PYTHONPATH=. /opt/homebrew/bin/python3 -m unittest -v tests/test_web.py
```
Expected: PASS.

- [ ] **Step 5: Commit**
```bash
git add ds940/web.py tests/test_web.py
git commit -m "feat(ui): implement macOS Finder window shell and Source List sidebar"
```

---

### Task 3: Dual-Mode Browser (Icon Grid & List View) with Path Breadcrumbs

**Files:**
- Modify: `ds940/web.py` (`serve_dashboard`)
- Test: `tests/test_web.py`

**Interfaces:**
- Produces: Dual-mode browser area (`#finder-main`), view switchers (`.view-toggle-grid`, `.view-toggle-list`), icon card renderer with folded-corner PDF badge, date badge, and selection ring.

- [ ] **Step 1: Write test assertion in `tests/test_web.py`**
```python
            self.assertIn("finder-grid", html)
            self.assertIn("view-toggle", html)
```

- [ ] **Step 2: Run test to verify it fails**
```bash
cd /Users/aaron/Projects/ds-940-paperless-scanner && PYTHONPATH=. /opt/homebrew/bin/python3 -m unittest -v tests/test_web.py
```
Expected: FAIL on missing `finder-grid`.

- [ ] **Step 3: Implement Icon Grid & List views in `ds940/web.py`**
1. CSS:
   - `.finder-grid`: CSS Grid with `grid-template-columns: repeat(auto-fill, minmax(130px, 1fr)); gap: 1rem;`
   - `.finder-icon-card`: macOS document icon with folded top-right corner, high-contrast PDF glyph, 2-line title clamping, date sublabel, and size badge.
   - macOS selection ring: blue translucent background `rgba(37, 99, 235, 0.15)` with focus outline.
   - `.finder-list`: compact macOS list view with header columns (Name, Date Scanned, Doc Date, Size, Category).
2. JS:
   - State variable `currentViewMode = 'grid' | 'list'`.
   - Toggle button event handlers with persistent preference in `localStorage.setItem('ds940-view-mode', ...)`.
   - Render cards in grid view or rows in list view based on `currentViewMode`.
   - Breadcrumb bar updates to `Mac Mini > brother-scans > [Current Folder]`.

- [ ] **Step 4: Run test to verify it passes**
```bash
cd /Users/aaron/Projects/ds-940-paperless-scanner && PYTHONPATH=. /opt/homebrew/bin/python3 -m unittest -v tests/test_web.py
```
Expected: PASS.

- [ ] **Step 5: Commit**
```bash
git add ds940/web.py tests/test_web.py
git commit -m "feat(ui): add macOS Icon Grid and List view modes with path breadcrumbs"
```

---

### Task 4: macOS Quick Look Inspector with Spacebar Toggle

**Files:**
- Modify: `ds940/web.py` (`serve_dashboard`)
- Test: `tests/test_web.py`

**Interfaces:**
- Produces: Quick Look inspector (`#quick-look-pane`), Spacebar keyboard event listener, PDF preview iframe, action toolbar (`Download`, `Open`, `Delete`).

- [ ] **Step 1: Write test assertion in `tests/test_web.py`**
```python
            self.assertIn("quick-look-pane", html)
```

- [ ] **Step 2: Run test to verify it fails**
```bash
cd /Users/aaron/Projects/ds-940-paperless-scanner && PYTHONPATH=. /opt/homebrew/bin/python3 -m unittest -v tests/test_web.py
```
Expected: FAIL on missing `quick-look-pane`.

- [ ] **Step 3: Implement Quick Look inspector and keyboard listeners in `ds940/web.py`**
1. Markup & CSS:
   - `#quick-look-pane`: Right-hand inspector pane displaying selected document title, category badge, document date, scan timestamp, file size, action buttons, and embedded preview iframe.
   - Quick Look toggle button in the toolbar with icon.
2. JS Keyboard Listeners:
   - When user presses **Spacebar** (`e.code === 'Space' || e.key === ' '`) while not in search input:
     - Toggle Quick Look pane visibility.
     - Prevent default scrolling behavior.
   - Arrow keys (`ArrowLeft`, `ArrowRight`, `ArrowUp`, `ArrowDown`) navigate the icon grid smoothly, automatically updating the active selection.

- [ ] **Step 4: Run test to verify it passes**
```bash
cd /Users/aaron/Projects/ds-940-paperless-scanner && PYTHONPATH=. /opt/homebrew/bin/python3 -m unittest -v tests/test_web.py
```
Expected: PASS.

- [ ] **Step 5: Commit**
```bash
git add ds940/web.py tests/test_web.py
git commit -m "feat(ui): add macOS Quick Look inspector with Spacebar shortcut"
```

---

### Task 5: Finder Statusbar Footer & Comprehensive Integration Testing

**Files:**
- Modify: `ds940/web.py`
- Modify: `scratch/capture_verification.py`
- Run: Full test suite (`85/85 tests`)

- [ ] **Step 1: Implement Finder Statusbar in `ds940/web.py`**
- `<footer class="finder-statusbar diagnostics-footer">` displaying:
  - Total items and folder size (*e.g., "7 items, 42.1 MB"*).
  - Scanner endpoint link (`BR5CF370DC7026.local:8080 ↗`).
  - Daemon status, process uptime, and RSS memory.
  - Local archive path with 1-click clipboard copy button.
- Ensure all IDs (`diag-scanner`, `diag-scanner-link`, `diag-state`, `diag-uptime`, `diag-ram`, `diag-path`, `strip-detail`) match existing definitions.

- [ ] **Step 2: Run full test suite**
```bash
cd /Users/aaron/Projects/ds-940-paperless-scanner && PYTHONPATH=. /opt/homebrew/bin/python3 -m unittest discover -s tests -v
```
Expected: PASS (all tests pass, 0 failures).

- [ ] **Step 3: Commit and push**
```bash
export PATH="/opt/homebrew/bin:$PATH" && cd /Users/aaron/Projects/ds-940-paperless-scanner && git commit -am "feat(ui): finalize macOS Finder desktop layout with statusbar footer" && git push origin main
```

- [ ] **Step 4: Deploy to remote host**
```bash
ssh -o ConnectTimeout=8 aaron@100.96.177.69 'cd ~/Projects/ds-940-paperless-scanner && git pull origin main && launchctl kickstart -k gui/$(id -u)/com.aaron.ds940receiver'
```

- [ ] **Step 5: Multi-viewport Playwright visual verification**
Run `scratch/capture_verification.py` across Desktop (1440px), Laptop (1024px), Tablet (768px), and Mobile (390px). Inspect screenshots to verify flawless visual rendering.
