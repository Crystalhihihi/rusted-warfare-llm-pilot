package rw.agent;

import java.util.ArrayList;

import android.graphics.Paint;
import android.graphics.RectF;

import com.corrodinggames.rts.gameFramework.l;
import com.corrodinggames.rts.gameFramework.m.y;

/**
 * AI 指挥台（聊天面板）：滚动消息区 + 底部输入框，标题栏可拖动、右下角可缩放。
 * - 消息缓冲（who/text/ts，上限 300 条）由网络/游戏线程写入，synchronized 保护；
 *   渲染线程只读快照，wrap 分行结果按（面板宽, 消息序号）缓存，仅渲染线程触碰
 * - 键盘输入由 OverlayInput（Hooks 注入）全权处理，本类只管绘制与几何/焦点状态展示
 * - 几何（px,py,pw,ph）为未缩放坐标 ×engine.cj（UI 密度）；游戏线程拖动写（volatile），渲染线程读
 */
final class ChatPanel {
    static volatile boolean visible = true; // 默认显示，主栏[聊天]切换
    static volatile float px = 30f;
    static volatile float py = 260f;
    static volatile float pw = 380f;
    static volatile float ph = 240f;

    static final float MIN_W = 260f;
    static final float MIN_H = 160f;
    private static final float INPUT_H = 28f;
    private static final float PAD = 6f;
    private static final float HANDLE = 16f;

    private ChatPanel() {
    }

    // ---------- 消息缓冲（写：网络/游戏线程；读：渲染线程快照） ----------

    private static final int MAX_MSGS = 300;
    private static final int MAX_TEXT = 2000; // 单条截断，给 wrap 的 O(n^2) 测宽兜底

    private static final class Msg {
        String who;
        String text;
        long ts;
        long sq; // 全局消息序号（增量 wrap 用）
    }

    private static final ArrayList<Msg> msgs = new ArrayList<Msg>();
    private static long msgSeq = 0L;        // synchronized(msgs) 内自增；快照缓存的失效键
    private static volatile int scroll = 0; // 距底行数：0=贴底（新消息自动可见），>0=用户上翻中

    static void add(String who, String text) {
        if (text == null || text.isEmpty()) {
            return;
        }
        if (who == null || who.isEmpty()) {
            who = "system";
        }
        if (text.length() > MAX_TEXT) {
            text = text.substring(0, MAX_TEXT);
        }
        Msg m = new Msg();
        m.who = who;
        m.text = text;
        m.ts = System.currentTimeMillis();
        synchronized (msgs) {
            m.sq = ++msgSeq;
            msgs.add(m);
            while (msgs.size() > MAX_MSGS) {
                msgs.remove(0);
            }
        }
        // 上翻时的视口抬高由增量 wrap 按实际行数处理（drawMessages）
    }

    private static long seq() {
        synchronized (msgs) {
            return msgSeq;
        }
    }

    private static Msg[] snapCache;
    private static long snapSeq = -1L;

    /** 渲染线程每帧取快照；消息无变更时复用缓存数组，零拷贝。 */
    private static Msg[] snapshot() {
        synchronized (msgs) {
            if (snapCache == null || snapSeq != msgSeq) {
                snapCache = msgs.toArray(new Msg[msgs.size()]);
                snapSeq = msgSeq;
            }
            return snapCache;
        }
    }

    // ---------- 滚动（OverlayData 滚轮路由调用，游戏线程） ----------

    static void scrollByLines(int n) {
        int v = scroll + n;
        scroll = Math.max(v, 0); // 上限由渲染线程按实际行数夹
    }

    // ---------- 几何/命中（游戏线程；mx,my 为真实屏幕像素） ----------

    private static float scaleOf() {
        l eng = l.B();
        return eng == null ? 1f : eng.cj; // cj=UI 密度（设置里的 UI 大小）；cX 是相机缩放，用了会跟地图变焦
    }

    static boolean scrollAreaContains(int mx, int my) {
        if (!visible) {
            return false;
        }
        float s = scaleOf();
        float pad = PAD * s;
        float x = px * s + pad;
        float y0 = py * s + Ui.TITLE_H * s + pad;
        float w = pw * s - 2 * pad;
        float h = ph * s - Ui.TITLE_H * s - INPUT_H * s - 3 * pad;
        return Ui.hit(x, y0, w, h, mx, my);
    }

    static boolean titleContains(int mx, int my, float s) {
        return visible && Ui.hit(px * s, py * s, pw * s, Ui.TITLE_H * s, mx, my);
    }

    static boolean inputContains(int mx, int my, float s) {
        if (!visible) {
            return false;
        }
        float pad = PAD * s;
        float ih = INPUT_H * s;
        return Ui.hit(px * s + pad, py * s + ph * s - pad - ih, pw * s - 2 * pad, ih, mx, my);
    }

    static boolean resizeContains(int mx, int my, float s) {
        if (!visible) {
            return false;
        }
        float hz = HANDLE * s;
        return Ui.hit(px * s + pw * s - hz, py * s + ph * s - hz, hz, hz, mx, my);
    }

    static boolean bodyContains(int mx, int my, float s) {
        return visible && Ui.hit(px * s, py * s, pw * s, ph * s, mx, my);
    }

    /** 拖动移动（未缩放坐标）；夹住不拖出屏幕（至少留 40px 可见）。 */
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

    /** 缩放（未缩放坐标）；最小 260x160，最大不超出屏幕。 */
    static void resizeTo(float nw, float nh) {
        if (nw < MIN_W) {
            nw = MIN_W;
        }
        if (nh < MIN_H) {
            nh = MIN_H;
        }
        try {
            float s = scaleOf();
            float maxW = org.lwjgl.opengl.Display.getWidth() / s - px;
            float maxH = org.lwjgl.opengl.Display.getHeight() / s - py;
            if (nw > maxW) {
                nw = maxW;
            }
            if (nh > maxH) {
                nh = maxH;
            }
        } catch (Throwable ignored) {
        }
        pw = nw;
        ph = nh;
    }

    // ---------- wrap 分行缓存（仅渲染线程；全量键=宽度px，增量按消息序号） ----------

    private static final class WLine {
        Paint color;
        String text;
        long msq; // 所属消息序号（溢出清理用）
    }

    private static final ArrayList<WLine> wlines = new ArrayList<WLine>();
    private static int wrapWidth = -1;
    private static long wrapDoneSq = 0L;    // 已 wrap 的最大消息序号（增量标记）
    private static int lastSeenWidth = -1;  // 上一帧宽度（检测拖动）
    private static long widthChgTs = 0L;    // 宽度最后变化时间（拖动冻结用）

    private static void addWLine(Paint color, String text, long msq) {
        WLine wl = new WLine();
        wl.color = color;
        wl.text = text;
        wl.msq = msq;
        wlines.add(wl);
    }

    /** 贪心逐字换行（CJK 无空格也能断）；widthPx 为真实像素宽。返回行数。 */
    private static int wrapPara(y g, String s, Paint color, int widthPx, long msq) {
        if (s.isEmpty()) {
            addWLine(color, "", msq); // 空行也占位（连续 \n）
            return 1;
        }
        int n = 0;
        StringBuilder cur = new StringBuilder(s.length());
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            cur.append(c);
            if (cur.length() > 1 && g.b(cur.toString(), color) > widthPx) {
                cur.setLength(cur.length() - 1);
                addWLine(color, cur.toString(), msq);
                n++;
                cur.setLength(0);
                cur.append(c);
            }
        }
        if (cur.length() > 0) {
            addWLine(color, cur.toString(), msq);
            n++;
        }
        return n;
    }

    private static int wrapMsg(y g, Msg m, int widthPx) {
        Paint color = Ui.rolePaint(m.who);
        String prefix = Ui.roleLabel(m.who) + ": ";
        String[] paras = m.text.split("\n", -1);
        int n = 0;
        for (int i = 0; i < paras.length; i++) {
            n += wrapPara(g, (i == 0 ? prefix : "") + paras[i], color, widthPx, m.sq);
        }
        return n;
    }

    /** 全量重排（仅宽度稳定后/缓存为空时调用——拖动中绝不进这里）。 */
    private static void rewrap(y g, Msg[] ms, int widthPx) {
        wlines.clear();
        long last = 0;
        for (Msg m : ms) {
            wrapMsg(g, m, widthPx);
            last = m.sq;
        }
        wrapWidth = widthPx;
        wrapDoneSq = last;
    }

    /** 增量 wrap：只处理新到的消息（宽度未变时）。返回新增行数（上翻视口抬高用）。 */
    private static int wrapNew(y g, Msg[] ms, int widthPx) {
        int added = 0;
        long last = wrapDoneSq;
        for (Msg m : ms) {
            if (m.sq > wrapDoneSq) {
                added += wrapMsg(g, m, widthPx);
                last = m.sq;
            }
        }
        wrapDoneSq = Math.max(wrapDoneSq, last);
        // 缓冲封顶挤掉旧消息后，清理对应旧行
        if (ms.length > 0) {
            long oldest = ms[0].sq;
            while (!wlines.isEmpty() && wlines.get(0).msq < oldest) {
                wlines.remove(0);
            }
        }
        return added;
    }

    // ---------- 绘制（渲染线程；错误局部吞，绝不拖累游戏） ----------

    static void draw(y g, float scale) {
        if (!visible) {
            return;
        }
        try {
            float x = px * scale;
            float y0 = py * scale;
            float w = pw * scale;
            float h = ph * scale;
            Ui.panel(g, x, y0, w, h);
            float th = Ui.TITLE_H * scale;
            Ui.titleBar(g, x, y0, w, th, scale, "AI 指挥台");
            if (scroll > 0) { // 上翻提示画在标题栏右侧
                String hint = "↑ 回看 " + scroll + " 行";
                float hw = g.b(hint, Ui.dimText);
                g.a(hint, x + w - hw - 8 * scale,
                    y0 + (th + Ui.dimText.k()) * 0.5f - Ui.dimText.k() * 0.2f, Ui.dimText);
            }
            drawMessages(g, scale, x, y0, w, h, th);
            drawInput(g, scale, x, y0, w, h);
            drawHandle(g, scale, x, y0, w, h);
        } catch (Throwable ignored) {
        }
    }

    private static void drawMessages(y g, float scale, float x, float y0, float w, float h, float th) {
        float pad = PAD * scale;
        float sx = x + pad;
        float sy = y0 + th + pad;
        float sw = w - 2 * pad;
        float sh = h - th - INPUT_H * scale - 3 * pad;
        Msg[] ms = snapshot();
        int widthPx = Math.max(40, (int) (sw - 4 * scale));
        long now = System.currentTimeMillis();
        if (widthPx != lastSeenWidth) {     // 宽度变了（拖动缩放中）：记录时间，等稳定后再重排
            lastSeenWidth = widthPx;
            widthChgTs = now;
        }
        if (widthPx != wrapWidth) {
            if (wlines.isEmpty() || now - widthChgTs > 250) {
                rewrap(g, ms, widthPx);     // 首次或宽度稳定 250ms：一次性全量重排
            }
            // 否则：拖动中，冻结旧排版（文字略微超宽/留白，松手 250ms 即恢复清晰）
        } else {
            int added = wrapNew(g, ms, widthPx); // 宽度未变：只增量 wrap 新消息（不再全量重排）
            if (added > 0 && scroll > 0) {
                scroll += added;                 // 上翻中：视口随新行抬高，不强制回底
            }
        }
        float lineH = (int) Ui.text.k() + 4;
        int rows = (int) (sh / lineH);
        int total = wlines.size();
        int maxScroll = Math.max(0, total - rows);
        if (scroll > maxScroll) {
            scroll = maxScroll;
        }
        g.k();
        g.a(new RectF(sx, sy, sx + sw, sy + sh)); // 滚动区裁剪
        try {
            if (total == 0) {
                g.a("（等待 AI 消息…）", sx, sy + lineH, Ui.dimText);
            } else {
                int end = total - scroll; // scroll=距底行数，画 [start,end)
                int start = Math.max(0, end - rows);
                float ty = sy + lineH - 3 * scale; // 首行 baseline
                for (int i = start; i < end; i++) {
                    WLine wl = wlines.get(i);
                    if (!wl.text.isEmpty()) {
                        g.a(wl.text, sx, ty, wl.color);
                    }
                    ty += lineH;
                }
            }
        } finally {
            g.l();
        }
    }

    private static void drawInput(y g, float scale, float x, float y0, float w, float h) {
        float pad = PAD * scale;
        float ih = INPUT_H * scale;
        float ix = x + pad;
        float iy = y0 + h - pad - ih;
        float iw = w - 2 * pad;
        Ui.rect(g, ix, iy, iw, ih, Ui.inputBg);
        Ui.border(g, ix, iy, iw, ih);
        float tx = ix + 6 * scale;
        float tb = iy + (ih + Ui.text.k()) * 0.5f - Ui.text.k() * 0.2f;
        if (OverlayInput.isFocused()) {
            String t = OverlayInput.text();
            int maxW = (int) (iw - 12 * scale);
            String shown = t;
            while (shown.length() > 0 && g.b(shown, Ui.text) > maxW) {
                shown = shown.substring(1); // 超长显示尾部（光标在末尾）
            }
            g.k();
            g.a(new RectF(ix, iy, ix + iw, iy + ih));
            try {
                g.a(shown, tx, tb, Ui.text);
                if (OverlayInput.caretOn()) {
                    float cx = tx + g.b(shown, Ui.text) + 1;
                    g.a(cx, iy + 5 * scale, cx, iy + ih - 5 * scale, Ui.caret);
                }
            } finally {
                g.l();
            }
        } else {
            g.a("Type to AI, Enter send, Esc close", tx, tb, Ui.dimText);
        }
    }

    /** 右下角缩放柄：三条短斜线。 */
    private static void drawHandle(y g, float scale, float x, float y0, float w, float h) {
        for (int i = 0; i < 3; i++) {
            float o = (5 + i * 4) * scale;
            g.a(x + w - o, y0 + h - 2.5f * scale, x + w - 2.5f * scale, y0 + h - o, Ui.dimText);
        }
    }
}
