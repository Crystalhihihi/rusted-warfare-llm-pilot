package rw.agent;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.Comparator;
import java.util.LinkedHashMap;
import java.util.concurrent.atomic.AtomicReference;

import android.graphics.Rect;
import android.graphics.RectF;

import com.corrodinggames.rts.game.units.am;
import com.corrodinggames.rts.game.units.ar;
import com.corrodinggames.rts.game.units.as;
import com.corrodinggames.rts.gameFramework.l;
import com.corrodinggames.rts.gameFramework.m.e;
import com.corrodinggames.rts.gameFramework.m.y;

/**
 * 委托部队面板（主栏[委托]开合，默认隐藏）：委托单位按类型分组的列表
 * （图标 24x24 + 类型名 x数量 + 聚合血条 Σcu/Σcv，受损>15% 条变红）。
 * - 单位快照由游戏线程每 1s 重建一次（AtomicReference 整表换出），渲染线程只读引用，绝不每帧遍历单位
 * - 顶部三按钮：全队接管(AiState.controlAll) / 委托选中(Commands.pickSelection) / 清空(delegatedIds)，
 *   动作后 AiState.notifyChange() 广播给 bridge
 * - 列表区 clip 裁剪 + 滚轮翻页（OverlayData 滚轮路由调 scrollByRows）
 */
final class DelegPanel {
    static volatile boolean visible = false; // 默认隐藏，主栏[委托]切换
    static volatile float px = 30f;          // 主栏下方
    static volatile float py = 150f;
    static volatile float pw = 380f;
    static volatile float ph = 300f;

    private static final float PAD = 6f;
    private static final float BTN_H = 26f;
    private static final float BTN_GAP = 6f;
    private static final int BTN_COUNT = 3;
    private static final float ROW_H = 30f;
    private static final float ICON = 24f;
    private static final float BAR_W = 90f;
    private static final float BAR_H = 8f;

    private static volatile int scroll = 0; // 距顶行数（向上翻页）

    private DelegPanel() {
    }

    // ---------- 单位快照（游戏线程 1s 重建；渲染线程只读） ----------

    /** 一行 = 一个单位类型的聚合；快照不可变，跨线程只读。 */
    static final class Row {
        String name;   // 类型名
        int count;     // 数量
        float hpFrac;  // Σcu/Σcv
        boolean damaged; // 受损比例 > 15%
        as type;       // 兜底 sprite / 后续扩展用
        am sample;     // 同类型任一单位（兜底 sprite 用）
        e icon;        // t.z()；内置单位为 null
    }

    private static final class Snap {
        final Row[] rows;
        final int total;

        Snap(Row[] rows, int total) {
            this.rows = rows;
            this.total = total;
        }
    }

    private static final Snap EMPTY = new Snap(new Row[0], 0);
    private static final AtomicReference<Snap> snap = new AtomicReference<Snap>(EMPTY);
    private static long nextRefresh = 0L;

    /** 游戏线程每 tick 调（OverlayData.clickPoll）；面板可见且到点才重建。 */
    static void tickRefresh(l engine) {
        if (!visible) {
            return; // 不动 nextRefresh：下次打开时立即刷新
        }
        long now = System.currentTimeMillis();
        if (now < nextRefresh) {
            return;
        }
        nextRefresh = now + 1000L;
        rebuild(engine);
    }

    /** 数据变更（按钮操作/面板打开）后调：下一 tick 立即重建。 */
    static void invalidate() {
        nextRefresh = 0L;
    }

    private static final class RowAcc {
        final Row row = new Row();
        float cu;
        float cv;
    }

    private static void rebuild(l engine) {
        try {
            if (engine == null || engine.bs == null || engine.bs.w) {
                return;
            }
            // 全队接管时 delegatedUnits 返回本队全部单位；这里按规格排除建筑
            ArrayList<am> units = Commands.delegatedUnits(engine.bs);
            LinkedHashMap<String, RowAcc> acc = new LinkedHashMap<String, RowAcc>();
            int total = 0;
            for (am u : units) {
                try {
                    if (u == null || u.bV || u.ej) {
                        continue;
                    }
                    as t = u.r();
                    if (t == null) {
                        continue;
                    }
                    if (AiState.controlAll) {
                        try {
                            if (t.j()) {
                                continue; // 建筑不进委托列表
                            }
                        } catch (Throwable ignored) {
                        }
                    }
                    String name;
                    try {
                        name = t.i();
                    } catch (Throwable ig) {
                        name = "?";
                    }
                    RowAcc a = acc.get(name);
                    if (a == null) {
                        a = new RowAcc();
                        a.row.name = name;
                        a.row.type = t;
                        a.row.sample = u;
                        try {
                            a.row.icon = t.z();
                        } catch (Throwable ig) {
                            a.row.icon = null;
                        }
                        acc.put(name, a);
                    }
                    a.row.count++;
                    a.cu += u.cu;
                    a.cv += u.cv;
                    total++;
                } catch (Throwable ignored) {
                }
            }
            Row[] rows = new Row[acc.size()];
            int i = 0;
            for (RowAcc a : acc.values()) {
                a.row.hpFrac = a.cv > 0f ? a.cu / a.cv : 1f;
                a.row.damaged = a.row.hpFrac < 0.85f;
                rows[i++] = a.row;
            }
            Arrays.sort(rows, new Comparator<Row>() { // 按类型名排序，显示稳定
                public int compare(Row ra, Row rb) {
                    return ra.name.compareToIgnoreCase(rb.name);
                }
            });
            snap.set(new Snap(rows, total));
        } catch (Throwable t) {
            Log.error("deleg snapshot failed", t);
        }
    }

    // ---------- 按钮动作（游戏线程，OverlayData 路由调用） ----------

    static void execBtn(l engine, int idx) {
        switch (idx) {
            case 0:
                AiState.controlAll = !AiState.controlAll;
                Log.info("deleg btn: controlAll=" + AiState.controlAll);
                break;
            case 1: {
                int added = Commands.pickSelection(engine.bs);
                Log.info("deleg btn: pick selection, added=" + added);
                break;
            }
            case 2:
                AiState.delegatedIds.clear();
                Log.info("deleg btn: clear delegated");
                break;
            default:
                return;
        }
        AiState.notifyChange();
        invalidate();
    }

    // ---------- 滚动（OverlayData 滚轮路由调用，游戏线程） ----------

    static void scrollByRows(int n) {
        int v = scroll + n;
        scroll = Math.max(v, 0); // 上限由渲染线程按实际行数夹
    }

    // ---------- 几何/命中（游戏线程；mx,my 为真实屏幕像素） ----------

    private static float scaleOf() {
        l eng = l.B();
        return eng == null ? 1f : eng.cj; // cj=UI 密度（设置里的 UI 大小）；cX 是相机缩放，用了会跟地图变焦
    }

    private static float btnW() {
        return (pw - 2 * PAD - (BTN_COUNT - 1) * BTN_GAP) / BTN_COUNT;
    }

    static boolean titleContains(int mx, int my, float s) {
        return visible && Ui.hit(px * s, py * s, pw * s, Ui.TITLE_H * s, mx, my);
    }

    /** 关闭 ×：标题栏右侧 TITLE_H 见方。 */
    static boolean closeContains(int mx, int my, float s) {
        if (!visible) {
            return false;
        }
        float th = Ui.TITLE_H * s;
        return Ui.hit(px * s + pw * s - th, py * s, th, th, mx, my);
    }

    static int btnAt(int mx, int my, float s) {
        if (!visible) {
            return -1;
        }
        float bw = btnW() * s;
        float by = (py + Ui.TITLE_H + PAD) * s;
        for (int i = 0; i < BTN_COUNT; i++) {
            float bx = (px + PAD + i * (btnW() + BTN_GAP)) * s;
            if (Ui.hit(bx, by, bw, BTN_H * s, mx, my)) {
                return i;
            }
        }
        return -1;
    }

    static boolean scrollAreaContains(int mx, int my) {
        if (!visible) {
            return false;
        }
        float s = scaleOf();
        float lx = (px + PAD) * s;
        float ly = (py + Ui.TITLE_H + PAD + BTN_H + PAD) * s;
        float lw = (pw - 2 * PAD) * s;
        float lh = (ph - Ui.TITLE_H - BTN_H - 3 * PAD) * s;
        return Ui.hit(lx, ly, lw, lh, mx, my);
    }

    static boolean bodyContains(int mx, int my, float s) {
        return visible && Ui.hit(px * s, py * s, pw * s, ph * s, mx, my);
    }

    /** 拖动移动（未缩放坐标）；夹住不拖出屏幕。 */
    static void moveTo(float nx, float ny) {
        if (nx < 0) {
            nx = 0;
        }
        if (ny < 0) {
            ny = 0;
        }
        try {
            float s = scaleOf();
            float maxX = org.lwjgl.opengl.Display.getWidth() / s - 40f;
            float maxY = org.lwjgl.opengl.Display.getHeight() / s - 40f;
            if (nx > maxX) {
                nx = maxX;
            }
            if (ny > maxY) {
                ny = maxY;
            }
        } catch (Throwable ignored) {
        }
        px = nx;
        py = ny;
    }

    // ---------- 绘制（渲染线程；错误局部吞） ----------

    static void draw(y g, float scale) {
        if (!visible) {
            return;
        }
        try {
            float x = px * scale;
            float y0 = py * scale;
            float w = pw * scale;
            float h = ph * scale;
            Snap s = snap.get();
            Ui.panel(g, x, y0, w, h);
            float th = Ui.TITLE_H * scale;
            String title = "委托部队 · 共" + s.total + (AiState.controlAll ? "（全队）" : "");
            Ui.titleBar(g, x, y0, w, th, scale, title);
            Ui.closeX(g, x, y0, w, th, scale);
            drawButtons(g, scale, x, y0, th);
            drawList(g, scale, x, y0, w, h, th, s);
        } catch (Throwable ignored) {
        }
    }

    private static void drawButtons(y g, float scale, float x, float y0, float th) {
        int delCount;
        synchronized (AiState.delegatedIds) {
            delCount = AiState.delegatedIds.size();
        }
        String[] labels = new String[BTN_COUNT];
        boolean[] on = new boolean[BTN_COUNT];
        labels[0] = AiState.controlAll ? "全队接管:开" : "全队接管:关";
        on[0] = AiState.controlAll;
        labels[1] = "委托选中(" + delCount + ")";
        on[1] = delCount > 0;
        labels[2] = "清空";
        on[2] = false;
        float bw = btnW() * scale;
        float by = y0 + th + PAD * scale;
        for (int i = 0; i < BTN_COUNT; i++) {
            float bx = x + (PAD + i * (btnW() + BTN_GAP)) * scale;
            Ui.button(g, bx, by, bw, BTN_H * scale, labels[i], on[i]);
        }
    }

    private static void drawList(y g, float scale, float x, float y0, float w, float h, float th, Snap s) {
        float pad = PAD * scale;
        float sx = x + pad;
        float sy = y0 + th + pad + BTN_H * scale + pad;
        float sw = w - 2 * pad;
        float sh = h - th - BTN_H * scale - 3 * pad;
        float rh = ROW_H * scale;
        int rowsFit = Math.max(1, (int) (sh / rh));
        Row[] rows = s.rows;
        int total = rows.length;
        int maxScroll = Math.max(0, total - rowsFit);
        if (scroll > maxScroll) {
            scroll = maxScroll;
        }
        g.k();
        g.a(new RectF(sx, sy, sx + sw, sy + sh)); // 列表区裁剪
        try {
            if (total == 0) {
                g.a("（无委托单位 — 框选后点[委托选中]）", sx, sy + rh * 0.7f, Ui.dimText);
            } else {
                float isz = ICON * scale;
                int end = Math.min(total, scroll + rowsFit + 1);
                for (int i = scroll; i < end; i++) {
                    drawRow(g, scale, rows[i], sx, sy + (i - scroll) * rh, sw, rh, isz);
                }
            }
        } finally {
            g.l();
        }
    }

    private static void drawRow(y g, float scale, Row row, float sx, float ry, float sw, float rh, float isz) {
        try {
            float iy = ry + (rh - isz) * 0.5f;
            if (row.icon != null) {
                try {
                    g.a(row.icon, new Rect(0, 0, row.icon.m(), row.icon.l()),
                        new RectF(sx, iy, sx + isz, iy + isz), Ui.imgPaint);
                } catch (Throwable ignored) {
                }
            } else if (row.type != null && row.sample != null) {
                try {
                    // 内置单位无图标：画真实 sprite（游戏 HUD 同款调用，用共享样板）
                    ar.a(row.type, sx + isz * 0.5f, iy + isz * 0.5f, 0, 0, row.sample.bX,
                        isz, isz, false, false, 0, row.sample);
                } catch (Throwable ignored) {
                }
            }
            // 类型名 x数量（过长截断加省略号）
            float bw = BAR_W * scale;
            float bx = sx + sw - bw - 2 * scale;
            String label = row.name + " x" + row.count;
            float tx = sx + isz + 6 * scale;
            float maxTw = bx - tx - 6 * scale;
            if (g.b(label, Ui.text) > maxTw) {
                while (label.length() > 1 && g.b(label + "…", Ui.text) > maxTw) {
                    label = label.substring(0, label.length() - 1);
                }
                label = label + "…";
            }
            float tb = ry + (rh + Ui.text.k()) * 0.5f - Ui.text.k() * 0.2f;
            g.a(label, tx, tb, Ui.text);
            // 聚合血条（右对齐；受损>15% 变红）
            float bh = BAR_H * scale;
            float by = ry + (rh - bh) * 0.5f;
            Ui.rect(g, bx, by, bw, bh, Ui.hpBack);
            float frac = row.hpFrac;
            if (frac < 0f) {
                frac = 0f;
            }
            if (frac > 1f) {
                frac = 1f;
            }
            if (frac > 0f) {
                Ui.rect(g, bx, by, bw * frac, bh, row.damaged ? Ui.hpBad : Ui.hpGood);
            }
        } catch (Throwable ignored) {
        }
    }
}
