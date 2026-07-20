package rw.agent;

import java.util.concurrent.atomic.AtomicReference;

import android.graphics.Paint;
import android.graphics.RectF;

import com.corrodinggames.rts.gameFramework.l;

/**
 * 游戏内 AI 面板：状态行 + LLM 想法文本 + 一排可点击按钮。
 * - 绘制在 OverlayObject.c(float)（渲染线程，世界 pass 顶层）
 * - 按钮点击用 LWJGL Mouse 静态 API 轮询（游戏线程，TickLoop 调 clickPoll）
 * - 跨线程数据只用 AtomicReference / AiState 的 volatile 字段
 */
public final class OverlayData {
    private static final AtomicReference<String[]> lines = new AtomicReference<String[]>(new String[0]);
    private static Paint bgPaint;
    private static Paint fgPaint;
    private static Paint btnPaint;
    private static Paint btnTextPaint;

    // 按钮布局（未缩放坐标，绘制时乘 cX）
    private static final int BTN_COUNT = 6;
    private static final float BX = 30;
    private static final float BY = 96;
    private static final float BW = 138;
    private static final float BH = 46;
    private static final float BGAP = 10;

    private OverlayData() {
    }

    static void handle(String line) {
        String text = Commands.strVal(line, "text");
        if (text == null || text.isEmpty()) {
            lines.set(new String[0]);
            return;
        }
        // pilot 用 ||| 作行分隔符（避免 JSON 转义层的歧义）
        lines.set(text.split(java.util.regex.Pattern.quote("|||")));
    }

    // ---------- 按钮点击（游戏线程，TickLoop 每 tick 调） ----------

    private static boolean mouseWasDown = false;

    public static void clickPoll() {
        try {
            boolean down = org.lwjgl.input.Mouse.isButtonDown(0);
            if (down && !mouseWasDown) {
                int mx = org.lwjgl.input.Mouse.getX();
                int my = org.lwjgl.opengl.Display.getHeight() - org.lwjgl.input.Mouse.getY();
                onClick(mx, my);
            }
            mouseWasDown = down;
        } catch (Throwable ignored) {
        }
    }

    private static void onClick(int mx, int my) {
        l engine = l.B();
        if (engine == null || engine.bs == null || engine.bs.w) {
            return;
        }
        float scale = engine.cX;
        for (int i = 0; i < BTN_COUNT; i++) {
            float x = (BX + i * (BW + BGAP)) * scale;
            float y = BY * scale;
            if (mx >= x && mx <= x + BW * scale && my >= y && my <= y + BH * scale) {
                switch (i) {
                    case 0:
                        AiState.enabled = !AiState.enabled;
                        Log.info("overlay button: AI " + (AiState.enabled ? "on" : "off"));
                        break;
                    case 1:
                        AiState.panelVisible = !AiState.panelVisible;
                        Log.info("overlay button: panel " + (AiState.panelVisible ? "on" : "off"));
                        break;
                    case 2:
                        // 节拍循环: 自动 -> 2.5 -> 6 -> 12 -> 自动
                        if (AiState.intervalOverride <= 0) {
                            AiState.intervalOverride = 2.5f;
                        } else if (AiState.intervalOverride < 4) {
                            AiState.intervalOverride = 6f;
                        } else if (AiState.intervalOverride < 9) {
                            AiState.intervalOverride = 12f;
                        } else {
                            AiState.intervalOverride = 0f;
                        }
                        Log.info("overlay button: interval=" + AiState.intervalOverride);
                        break;
                    case 3: {
                        int added = Commands.pickSelection(engine.bs);
                        Log.info("overlay button: pick selection, added=" + added);
                        break;
                    }
                    case 4:
                        AiState.delegatedIds.clear();
                        Log.info("overlay button: clear delegated");
                        break;
                    case 5:
                        AiState.controlAll = !AiState.controlAll;
                        Log.info("overlay button: controlAll=" + AiState.controlAll);
                        break;
                    default:
                        break;
                }
                AiState.notifyChange();
                return;
            }
        }
    }

    // ---------- 绘制（渲染线程） ----------

    static void draw() {
        l engine = l.B();
        if (engine == null || engine.bO == null || engine.bs == null || engine.bs.w) {
            return; // 菜单/AI 混战不显示
        }
        if (bgPaint == null) {
            bgPaint = new Paint();
            bgPaint.a(140, 0, 0, 0);
            fgPaint = new Paint();
            fgPaint.a(255, 240, 240, 240);
            btnPaint = new Paint();
            btnPaint.a(190, 30, 45, 60);
            btnTextPaint = new Paint();
            btnTextPaint.a(255, 255, 255, 255);
        }
        com.corrodinggames.rts.gameFramework.m.y g = engine.bO;
        float scale = engine.cX;
        g.k();
        engine.S(); // 撤销相机缩放，画屏固尺寸（游戏自带血条同款模式）
        try {
            drawButtons(g, scale);
            if (AiState.panelVisible) {
                drawLines(g, scale);
            }
        } finally {
            g.l();
        }
    }

    private static void drawButtons(com.corrodinggames.rts.gameFramework.m.y g, float scale) {
        int delCount;
        synchronized (AiState.delegatedIds) {
            delCount = AiState.delegatedIds.size();
        }
        String[] labels = new String[BTN_COUNT];
        labels[0] = AiState.enabled ? "AI:开" : "AI:关";
        labels[1] = AiState.panelVisible ? "面板:开" : "面板:关";
        labels[2] = "节拍:" + (AiState.intervalOverride <= 0 ? "自动" : ((int) AiState.intervalOverride + "s"));
        labels[3] = "委托选中(" + delCount + ")";
        labels[4] = "清空委托";
        labels[5] = AiState.controlAll ? "全队:开" : "全队:关";
        for (int i = 0; i < BTN_COUNT; i++) {
            float x = (BX + i * (BW + BGAP)) * scale;
            float y = BY * scale;
            RectF r = new RectF();
            r.a(x, y, x + BW * scale, y + BH * scale);
            g.a(r, btnPaint);
            g.a(labels[i], x + 10 * scale, y + 30 * scale, btnTextPaint);
        }
    }

    private static void drawLines(com.corrodinggames.rts.gameFramework.m.y g, float scale) {
        String[] ls = lines.get();
        if (ls.length == 0) {
            return;
        }
        float x = 30 * scale;
        float y = (BY + BH + 14) * scale;
        int shown = 0;
        for (String s : ls) {
            if (s == null || s.isEmpty()) {
                continue;
            }
            // 实测：第一个 Paint=文字色，第二个=底板色
            g.a(s, x, y, fgPaint, bgPaint, 6.0f);
            y += 34 * scale;
            if (++shown >= 10) {
                break;
            }
        }
    }
}
