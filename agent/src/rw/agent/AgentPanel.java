package rw.agent;

import com.corrodinggames.rts.gameFramework.l;
import com.corrodinggames.rts.gameFramework.m.y;

/**
 * AI 设置面板（主栏[设置]开合，默认隐藏）：三个角色开关行
 * [总指挥:开/关] [建造:开/关] [战斗:开/关]，点击切换 AiState.cmdCommander/cmdBuild/cmdCombat
 * 并 AiState.notifyChange() 广播给 bridge。
 * - 风格与 DelegPanel 一致：Ui 骨架（底色/描边/标题栏），字号随 engine.cj（UI 密度）
 * - 内容仅三个 volatile 布尔，绘制时直接读，无需快照；无滚动区
 * - 标题栏可拖动、右上关闭 ×；几何为未缩放坐标 ×cj，游戏线程写渲染线程读
 */
final class AgentPanel {
    static volatile boolean visible = false; // 默认隐藏，主栏[设置]切换
    static volatile float px = 430f;         // 主栏下方偏右（避开 DelegPanel 默认位）
    static volatile float py = 150f;
    static volatile float pw = 230f;
    static volatile float ph = 140f;

    private static final float PAD = 6f;
    private static final float ROW_H = 30f;
    private static final float ROW_GAP = 6f;
    private static final int ROW_COUNT = 3;

    private AgentPanel() {
    }

    // ---------- 开关动作（游戏线程，OverlayData 路由调用） ----------

    static void execRow(int idx) {
        switch (idx) {
            case 0:
                AiState.cmdCommander = !AiState.cmdCommander;
                Log.info("agent panel: cmdCommander=" + AiState.cmdCommander);
                break;
            case 1:
                AiState.cmdBuild = !AiState.cmdBuild;
                Log.info("agent panel: cmdBuild=" + AiState.cmdBuild);
                break;
            case 2:
                AiState.cmdCombat = !AiState.cmdCombat;
                Log.info("agent panel: cmdCombat=" + AiState.cmdCombat);
                break;
            default:
                return;
        }
        AiState.notifyChange();
    }

    // ---------- 几何/命中（游戏线程；mx,my 为真实屏幕像素） ----------

    private static float scaleOf() {
        l eng = l.B();
        return eng == null ? 1f : eng.cj; // cj=UI 密度（设置里的 UI 大小）；cX 是相机缩放，用了会跟地图变焦
    }

    private static float rowY(int i, float s) {
        return (py + Ui.TITLE_H + PAD + i * (ROW_H + ROW_GAP)) * s;
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

    static int rowAt(int mx, int my, float s) {
        if (!visible) {
            return -1;
        }
        float rx = (px + PAD) * s;
        float rw = (pw - 2 * PAD) * s;
        for (int i = 0; i < ROW_COUNT; i++) {
            if (Ui.hit(rx, rowY(i, s), rw, ROW_H * s, mx, my)) {
                return i;
            }
        }
        return -1;
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
            Ui.titleBar(g, x, y0, w, th, scale, "AI 设置");
            Ui.closeX(g, x, y0, w, th, scale);
            String[] labels = new String[ROW_COUNT];
            boolean[] on = new boolean[ROW_COUNT];
            labels[0] = AiState.cmdCommander ? "总指挥:开" : "总指挥:关";
            on[0] = AiState.cmdCommander;
            labels[1] = AiState.cmdBuild ? "建造:开" : "建造:关";
            on[1] = AiState.cmdBuild;
            labels[2] = AiState.cmdCombat ? "战斗:开" : "战斗:关";
            on[2] = AiState.cmdCombat;
            float rx = x + PAD * scale;
            float rw = w - 2 * PAD * scale;
            for (int i = 0; i < ROW_COUNT; i++) {
                Ui.button(g, rx, y0 + (Ui.TITLE_H + PAD + i * (ROW_H + ROW_GAP)) * scale,
                    rw, ROW_H * scale, labels[i], on[i]);
            }
        } catch (Throwable ignored) {
        }
    }
}
