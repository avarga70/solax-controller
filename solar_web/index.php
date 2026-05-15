<?php
/**
 * Solar Controller Web UI
 * Served at http://vip.home.akos.name/solar/
 *
 * Credentials are read from /etc/solax/db.php (outside web root).
 * That file must define: $sqlite_path
 */

require_once '/etc/solax/db.php';

// ── DB connection ──────────────────────────────────────────────────────────
function db(): PDO {
    global $sqlite_path;
    static $conn = null;
    if ($conn === null) {
        try {
            $conn = new PDO('sqlite:' . $sqlite_path, null, null, [
                PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION,
                PDO::ATTR_DEFAULT_FETCH_MODE => PDO::FETCH_ASSOC,
            ]);
            $conn->exec("PRAGMA journal_mode=WAL");
            $conn->exec("PRAGMA busy_timeout=10000");
        } catch (PDOException $e) {
            die("DB error: " . htmlspecialchars($e->getMessage()));
        }
    }
    return $conn;
}

// ── Handle POST actions ────────────────────────────────────────────────────
$message = '';
if ($_SERVER['REQUEST_METHOD'] === 'POST') {
    $action = $_POST['action'] ?? '';

    if ($action === 'set_auto') {
        db()->exec("UPDATE solax_control SET manual_mode=0, updated_by='web-ui', updated_at=CURRENT_TIMESTAMP WHERE id=1");
        $message = 'Switched to AUTO — controller resumed.';

    } elseif ($action === 'set_manual') {
        $forced_mode    = in_array($_POST['forced_mode'] ?? '', ['ECO_CHARGE','BACKUP','GENERAL'])
                          ? $_POST['forced_mode'] : null;
        $forced_min_soc = is_numeric($_POST['forced_min_soc'] ?? '')
                          ? max(10, min(98, (int)$_POST['forced_min_soc'])) : null;
        $stmt = db()->prepare(
            "UPDATE solax_control
             SET manual_mode=1, forced_mode=?, forced_min_soc=?, updated_by='web-ui', updated_at=CURRENT_TIMESTAMP
             WHERE id=1"
        );
        $stmt->execute([$forced_mode, $forced_min_soc]);
        $message = 'Manual override active — inverter writes suppressed.';
    }
    // PRG redirect to avoid form re-submit on refresh
    header("Location: " . $_SERVER['PHP_SELF'] . "?msg=" . urlencode($message));
    exit;
}
if (isset($_GET['msg'])) $message = htmlspecialchars($_GET['msg']);

// ── Fetch data ─────────────────────────────────────────────────────────────
$ctrl = db()->query("SELECT * FROM solax_control WHERE id=1")->fetch();

$last = db()->query("
    SELECT * FROM solax_decisions ORDER BY logged_at DESC LIMIT 1
")->fetch();

try {
    $run = db()->query("
        SELECT * FROM PV_run ORDER BY pdtime DESC LIMIT 1
    ")->fetch();
} catch (PDOException $e) {
    $run = null; // PV_run table not yet created (poller hasn't connected to inverter)
}

// Show only rows where mode or min SOC changed from the previous decision.
// LAG() over all recent rows detects the transitions; limit to last 48 h so
// a full day of changes is always visible even on quiet nights.
$history = db()->query("
    SELECT logged_at, node_name, is_leader, test_mode,
           soc_pct, pv_w, grid_w, price_now,
           current_mode, target_mode, target_min_soc, reason
    FROM (
        SELECT *,
               LAG(target_mode)    OVER (ORDER BY logged_at) AS prev_mode,
               LAG(target_min_soc) OVER (ORDER BY logged_at) AS prev_soc
        FROM solax_decisions
        WHERE logged_at >= datetime('now', '-48 hours')
    ) t
    WHERE prev_mode IS NULL OR prev_mode != target_mode OR prev_soc != target_min_soc
    ORDER BY logged_at DESC
    LIMIT 50
");

// ── Helpers ────────────────────────────────────────────────────────────────
function badge(string $text, string $color): string {
    return "<span class=\"badge badge-$color\">$text</span>";
}
function fmt_w(mixed $w): string {
    if ($w === null) return '—';
    $w = (int)$w;
    return ($w >= 0 ? '+' : '') . number_format($w) . ' W';
}
function fmt_ts(string $dt): string {
    return date('d.m H:i:s', strtotime($dt));
}

$manual = (bool)($ctrl['manual_mode'] ?? false);

// Distribution fee constants (match controller defaults)
$dist_peak_hours = [8, 12, 15, 19];
$dist_fee_normal = 140.97;
$dist_fee_peak   = 913.27;
$dist_delta      = $dist_fee_peak - $dist_fee_normal; // ~772 CZK/MWh
$last_hour       = $last ? (int)date('G', strtotime($last['logged_at'])) : null;
$is_dist_peak    = $last_hour !== null && in_array($last_hour, $dist_peak_hours);
$effective_price = ($last && $last['price_now'] !== null)
                   ? (float)$last['price_now'] + ($is_dist_peak ? $dist_delta : 0)
                   : null;
?>
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="60">
<title>Solar Controller</title>
<style>
  :root {
    --green:  #22c55e; --red: #ef4444; --yellow: #f59e0b;
    --blue:   #3b82f6; --gray: #6b7280; --bg: #0f172a;
    --card:   #1e293b; --border: #334155; --text: #f1f5f9;
    --muted:  #94a3b8;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: system-ui, sans-serif; background: var(--bg); color: var(--text);
         font-size: 15px; padding: 16px; }
  h1   { font-size: 1.4rem; font-weight: 700; margin-bottom: 16px; }
  h2   { font-size: 1rem; font-weight: 600; color: var(--muted); margin-bottom: 10px; text-transform: uppercase; letter-spacing: .05em; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 12px; margin-bottom: 12px; }
  .card { background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 16px; }
  .stat-row { display: flex; justify-content: space-between; align-items: center;
              padding: 5px 0; border-bottom: 1px solid var(--border); }
  .stat-row:last-child { border-bottom: none; }
  .stat-label { color: var(--muted); font-size: .85rem; }
  .stat-val   { font-weight: 600; }
  .badge { display: inline-block; padding: 2px 10px; border-radius: 20px; font-size: .8rem; font-weight: 700; }
  .badge-green  { background: #14532d; color: var(--green); }
  .badge-red    { background: #7f1d1d; color: #fca5a5; }
  .badge-yellow { background: #78350f; color: var(--yellow); }
  .badge-blue   { background: #1e3a5f; color: #93c5fd; }
  .badge-gray   { background: #1f2937; color: var(--muted); }
  .msg  { background: #1e3a5f; border: 1px solid var(--blue); border-radius: 8px;
          padding: 10px 14px; margin-bottom: 12px; color: #93c5fd; }
  form  { display: flex; flex-direction: column; gap: 10px; }
  label { font-size: .85rem; color: var(--muted); }
  select, input[type=number] {
    background: #0f172a; border: 1px solid var(--border); color: var(--text);
    border-radius: 6px; padding: 6px 10px; width: 100%; font-size: .95rem; }
  .btn { padding: 8px 18px; border: none; border-radius: 7px; cursor: pointer;
         font-weight: 600; font-size: .9rem; }
  .btn-green  { background: var(--green);  color: #052e16; }
  .btn-red    { background: var(--red);    color: #fff; }
  .btn-yellow { background: var(--yellow); color: #1c0500; }
  table { width: 100%; border-collapse: collapse; font-size: .82rem; }
  th { text-align: left; color: var(--muted); font-weight: 600; padding: 6px 8px;
       border-bottom: 1px solid var(--border); white-space: nowrap; }
  td { padding: 5px 8px; border-bottom: 1px solid #1e293b; vertical-align: top; }
  tr:hover td { background: #1e293b; }
  .reason { color: var(--muted); font-size: .78rem; max-width: 360px; }
  .soc-bar { height: 6px; background: var(--border); border-radius: 3px; margin-top: 4px; }
  .soc-fill { height: 100%; border-radius: 3px; background: var(--green); transition: width .4s; }
</style>
</head>
<body>
<h1><a href="<?= htmlspecialchars($_SERVER['PHP_SELF']) ?>" style="color:inherit;text-decoration:none">☀️ Solar Controller</a></h1>

<?php if ($message): ?>
<div class="msg"><?= $message ?></div>
<?php endif; ?>

<div class="grid">

  <!-- Live inverter state -->
  <div class="card">
    <h2>Inverter State <?php if($run): ?><small style="font-size:.75rem;color:var(--muted)">(<?= fmt_ts($run['pdtime']) ?>)</small><?php endif ?></h2>
    <?php
      $soc = $run ? (float)$run['pbatts'] : null;
      $mode_badges = ['ECO_CHARGE'=>'green','BACKUP'=>'yellow','GENERAL'=>'gray'];
    ?>
    <div class="stat-row">
      <span class="stat-label">Battery SOC</span>
      <span class="stat-val"><?= $soc !== null ? $soc.'%' : '—' ?></span>
    </div>
    <?php if ($soc !== null): ?>
    <div class="soc-bar"><div class="soc-fill" style="width:<?= min(100,$soc) ?>%"></div></div>
    <?php endif ?>
    <div class="stat-row" style="margin-top:6px">
      <span class="stat-label">Battery</span>
      <span class="stat-val"><?= $run ? fmt_w($run['pbattw']) : '—' ?></span>
    </div>
    <div class="stat-row">
      <span class="stat-label">PV</span>
      <span class="stat-val"><?= $run ? '+'.number_format((int)$run['ppv']).' W' : '—' ?></span>
    </div>
    <div class="stat-row">
      <span class="stat-label">Grid</span>
      <span class="stat-val"><?= $run ? fmt_w($run['pgridT']) : '—' ?></span>
    </div>
    <div class="stat-row">
      <span class="stat-label">House load</span>
      <span class="stat-val"><?= $run ? number_format((int)$run['ploadT']).' W' : '—' ?></span>
    </div>
  </div>

  <!-- Last controller decision -->
  <div class="card">
    <h2>Last Decision <?php if($last): ?><small style="font-size:.75rem;color:var(--muted)">(<?= fmt_ts($last['logged_at']) ?>)</small><?php endif ?></h2>
    <?php if ($last): ?>
    <div class="stat-row">
      <span class="stat-label">Controller mode</span>
      <?php $mc = $mode_badges[$last['target_mode']] ?? 'gray'; ?>
      <span><?= badge($last['target_mode'], $mc) ?></span>
    </div>
    <div class="stat-row">
      <span class="stat-label">Min SOC</span>
      <span class="stat-val"><?= $last['target_min_soc'] ?>%</span>
    </div>
    <div class="stat-row">
      <span class="stat-label">Price now</span>
      <span class="stat-val">
        <?php if ($last['price_now'] !== null): ?>
          <?= number_format((float)$last['price_now']) ?> CZK/MWh
          <?php if ($is_dist_peak): ?>
          <br><span style="color:var(--yellow);font-size:.78rem">
            +<?= number_format($dist_delta) ?> dist. peak → <strong><?= number_format($effective_price) ?></strong> eff.
          </span>
          <?php endif ?>
        <?php else: ?>—<?php endif ?>
      </span>
    </div>
    <div class="stat-row">
      <span class="stat-label">Node / test</span>
      <span class="stat-val"><?= htmlspecialchars($last['node_name']) ?> <?= $last['test_mode'] ? badge('TEST','yellow') : badge('LIVE','green') ?></span>
    </div>
    <div style="margin-top:8px;font-size:.8rem;color:var(--muted);line-height:1.5">
      <?= htmlspecialchars($last['reason'] ?? '') ?>
    </div>
    <?php else: ?>
    <p style="color:var(--muted)">No decisions logged yet.</p>
    <?php endif ?>
  </div>

  <!-- Manual override control -->
  <div class="card">
    <h2>Control</h2>
    <div class="stat-row" style="margin-bottom:12px">
      <span class="stat-label">Mode</span>
      <?= $manual ? badge('MANUAL','red') : badge('AUTO','green') ?>
    </div>
    <?php if ($manual): ?>
    <div class="stat-row">
      <span class="stat-label">Forced mode</span>
      <span class="stat-val"><?= htmlspecialchars($ctrl['forced_mode'] ?? '—') ?></span>
    </div>
    <div class="stat-row" style="margin-bottom:14px">
      <span class="stat-label">Forced min SOC</span>
      <span class="stat-val"><?= $ctrl['forced_min_soc'] ?? '—' ?>%</span>
    </div>
    <form method="post">
      <input type="hidden" name="action" value="set_auto">
      <button class="btn btn-green" type="submit">▶ Resume AUTO</button>
    </form>
    <hr style="border-color:var(--border);margin:14px 0">
    <form method="post">
      <input type="hidden" name="action" value="set_manual">
      <label>Mode to force</label>
      <select name="forced_mode">
        <option value="GENERAL"    <?= ($ctrl['forced_mode']==='GENERAL'   ?'selected':'') ?>>GENERAL (normal)</option>
        <option value="ECO_CHARGE" <?= ($ctrl['forced_mode']==='ECO_CHARGE'?'selected':'') ?>>ECO_CHARGE (charge from grid)</option>
        <option value="BACKUP"     <?= ($ctrl['forced_mode']==='BACKUP'    ?'selected':'') ?>>BACKUP (hold battery)</option>
      </select>
      <label>Min SOC %</label>
      <input type="number" name="forced_min_soc" min="10" max="98"
             value="<?= htmlspecialchars($ctrl['forced_min_soc'] ?? '30') ?>">
      <button class="btn btn-yellow" type="submit">⚙ Update override</button>
    </form>
    <?php else: ?>
    <p style="font-size:.85rem;color:var(--muted);margin-bottom:14px">
      Controller is running autonomously. Switch to manual to take over.</p>
    <form method="post">
      <input type="hidden" name="action" value="set_manual">
      <label>Mode to force</label>
      <select name="forced_mode">
        <option value="GENERAL">GENERAL (normal)</option>
        <option value="ECO_CHARGE">ECO_CHARGE (charge from grid)</option>
        <option value="BACKUP">BACKUP (hold battery)</option>
      </select>
      <label>Min SOC %</label>
      <input type="number" name="forced_min_soc" min="10" max="98" value="30">
      <button class="btn btn-red" type="submit">✋ Take manual control</button>
    </form>
    <?php endif ?>
  </div>

</div>

<!-- Decision history -->
<div class="card">
  <h2>Decision Changes <small style="font-size:.75rem;color:var(--muted);text-transform:none;letter-spacing:0">— last 48 h, only when mode or min SOC changed</small></h2>
  <div style="overflow-x:auto">
  <table>
    <tr>
      <th>Time</th><th>Node</th><th>SOC</th><th>PV W</th><th>Grid W</th>
      <th>Price</th><th>Was</th><th>→ Mode</th><th>MinSOC</th><th>Reason</th>
    </tr>
    <?php while ($row = $history->fetch()): ?>
    <tr>
      <td style="white-space:nowrap"><?= date('d.m H:i', strtotime($row['logged_at'])) ?></td>
      <td><?= htmlspecialchars($row['node_name']) ?><?= $row['test_mode'] ? ' <span style="color:var(--yellow)">T</span>' : '' ?></td>
      <td><?= $row['soc_pct'] ?>%</td>
      <td><?= $row['pv_w'] !== null ? '+'.number_format((int)$row['pv_w']) : '—' ?></td>
      <td><?= $row['grid_w'] !== null ? fmt_w($row['grid_w']) : '—' ?></td>
      <td style="white-space:nowrap"><?= $row['price_now'] !== null ? number_format((float)$row['price_now']) : '—' ?></td>
      <td><span class="badge badge-<?= $mode_badges[$row['current_mode']] ?? 'gray' ?>"><?= htmlspecialchars($row['current_mode'] ?? '—') ?></span></td>
      <td><span class="badge badge-<?= $mode_badges[$row['target_mode']] ?? 'gray' ?>"><?= htmlspecialchars($row['target_mode']) ?></span></td>
      <td><?= $row['target_min_soc'] ?>%</td>
      <td class="reason"><?= htmlspecialchars(mb_strimwidth($row['reason'] ?? '', 0, 120, '…')) ?></td>
    </tr>
    <?php endwhile ?>
  </table>
  </div>
</div>

<p style="margin-top:12px;font-size:.75rem;color:var(--muted)">
  Auto-refreshes every 60 s &nbsp;·&nbsp; <?= date('H:i:s') ?>
</p>
</body>
</html>
