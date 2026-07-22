package rw.agent;

import java.util.concurrent.atomic.AtomicReference;

import com.corrodinggames.rts.gameFramework.l;
import com.corrodinggames.rts.gameFramework.m.y;

/**
 * 游戏内 overlay 总控：主栏（[AI] [节拍] [聊天] [委托] [设置] [面板]）+ 主栏下方状态行，
 * 并把鼠标按下/拖动/滚轮路由到 ChatPanel（AI 指挥台）、DelegPanel（委托部队）与 AgentPanel（AI 设置）。
 * - 绘制在 OverlayObject.c(float)（渲染线程，世界 pass 顶层）
 * - 交互用 LWJGL Mouse 静态 API 轮询（游戏线程，TickLoop 每 tick 调 clickPoll）
 * - 桥接协议：{"cmd":"overlay","text":"l1|||l2"} 状态行；
 *   {"cmd":"overlay","who":"commander|build|combat|system","chat":"文本"} 聊天消息
 * - 跨线程数据只用 AtomicReference / volatile / synchronized 快照，渲染线程只读
 */
public final class OverlayData {
    private static final AtomicReference<String[]> lines = new AtomicReference<String[]>(new String[0]);

    // 主栏布局（未缩放坐标，绘制/命中时 × engine.cj——UI 密度；禁用 cX，那是相机缩放）
    private static final int BTN_COUNT = 6; // [AI] [节拍] [聊天] [委托] [设置] [面板]
    private static final float BX = 30;
    private static final float BY = 96;
    private static final float BW = 128;
    private static final float BH = 42;
    private static final float BGAP = 8;
    private static final float MINI_W = 96;  // 面板:关 时只留的小按钮
    private static final float MINI_H = 30;

    private OverlayData() {
    }

    // ---------- 桥接协议（游戏线程，Commands.handle 分发） ----------

    static void handle(String line) {
        try {
            String chat = Commands.strVal(line, "chat");
            if (chat != null) {
                String who = Commands.strVal(line, "who");
                addChat(who == null ? "system" : who, chat);
                return;
            }
            String text = Commands.strVal(line, "text");
            if (text == null || text.isEmpty()) {
                lines.set(new String[0]);
                return;
            }
            // pilot 用 ||| 作行分隔符（避免 JSON 转义层的歧义）
            lines.set(text.split(java.util.regex.Pattern.quote("|||")));
        } catch (Throwable t) {
            Log.error("overlay handle failed", t);
        }
    }

    /** 追加聊天消息（OverlayInput 玩家回显 / bridge who+chat 协议）。 */
    public static void addChat(String who, String text) {
        try {
            ChatPanel.add(who, text);
        } catch (Throwable ignored) {
        }
    }

    // ---------- 滚轮 hook（OverlayInput 回调，游戏线程） ----------

    /** 屏幕坐标是否落在任一面板的滚动区内（聊天消息区 / 委托列表区）。 */
    public static boolean chatScrollAreaContains(int mx, int my) {
        try {
            if (!AiState.panelVisible) {
                return false;
            }
            return ChatPanel.scrollAreaContains(mx, my) || DelegPanel.scrollAreaContains(mx, my);
        } catch (Throwable t) {
            return false;
        }
    }

    /** 滚行数（正=回看历史）；按当前光标位置路由到对应面板。 */
    public static void chatScrollBy(int n) {
        try {
            int mx = org.lwjgl.input.Mouse.getX();
            int my = org.lwjgl.opengl.Display.getHeight() - org.lwjgl.input.Mouse.getY();
            if (ChatPanel.scrollAreaContains(mx, my)) {
                ChatPanel.scrollByLines(n);
            } else if (DelegPanel.scrollAreaContains(mx, my)) {
                DelegPanel.scrollByRows(n);
            }
        } catch (Throwable ignored) {
        }
    }

    // ---------- 交互（游戏线程，TickLoop 每 tick 调） ----------

    private static boolean mouseWasDown = false;
    private static final int DRAG_NONE = 0;
    private static final int DRAG_CHAT_MOVE = 1;
    private static final int DRAG_CHAT_RESIZE = 2;
    private static final int DRAG_DELEG_MOVE = 3;
    private static final int DRAG_AGENT_MOVE = 4;
    private static int dragMode = DRAG_NONE;
    private static float dragDX; // 移动：鼠标→面板锚点偏移（未缩放）
    private static float dragDY;
    private static float rsW;    // 缩放：按下时的起点快照
    private static float rsH;
    private static float rsMX;
    private static float rsMY;
    private static float rsScale;

    public static void clickPoll() {
        try {
            l engine = l.B();
            boolean down = org.lwjgl.input.Mouse.isButtonDown(0);
            int mx = org.lwjgl.input.Mouse.getX();
            int my = org.lwjgl.opengl.Display.getHeight() - org.lwjgl.input.Mouse.getY();
            if (down && !mouseWasDown) {
                if (engine != null && engine.bs != null && !engine.bs.w) {
                    press(engine, mx, my);
                }
            } else if (down && dragMode != DRAG_NONE) {
                drag(engine, mx, my);
            } else if (!down && mouseWasDown) {
                dragMode = DRAG_NONE;
            }
            mouseWasDown = down;
            DelegPanel.tickRefresh(engine); // 委托快照 1s 节流重建
        } catch (Throwable ignored) {
        }
    }

    /** 按下边沿：标题栏→开始拖动；按钮→执行；输入框→聚焦；缩放柄→开始缩放；其他→失焦输入框。 */
    private static void press(l engine, int mx, int my) {
        float s = engine.cj; // cj=UI 密度；cX 是相机缩放（禁用：会跟地图变焦）
        if (mainBarPress(engine, mx, my, s)) {
            return;
        }
        if (!AiState.panelVisible) {
            OverlayInput.setFocused(false);
            return;
        }
        if (DelegPanel.visible) {
            if (DelegPanel.closeContains(mx, my, s)) {
                DelegPanel.visible = false;
                Log.info("deleg panel closed");
                return;
            }
            if (DelegPanel.titleContains(mx, my, s)) {
                dragMode = DRAG_DELEG_MOVE;
                dragDX = mx / s - DelegPanel.px;
                dragDY = my / s - DelegPanel.py;
                return;
            }
            int b = DelegPanel.btnAt(mx, my, s);
            if (b >= 0) {
                DelegPanel.execBtn(engine, b);
                return;
            }
            if (DelegPanel.bodyContains(mx, my, s)) {
                OverlayInput.setFocused(false);
                return;
            }
        }
        if (AgentPanel.visible) {
            if (AgentPanel.closeContains(mx, my, s)) {
                AgentPanel.visible = false;
                Log.info("agent panel closed");
                return;
            }
            if (AgentPanel.titleContains(mx, my, s)) {
                dragMode = DRAG_AGENT_MOVE;
                dragDX = mx / s - AgentPanel.px;
                dragDY = my / s - AgentPanel.py;
                return;
            }
            int r = AgentPanel.rowAt(mx, my, s);
            if (r >= 0) {
                AgentPanel.execRow(r);
                return;
            }
            if (AgentPanel.bodyContains(mx, my, s)) {
                OverlayInput.setFocused(false);
                return;
            }
        }
        if (ChatPanel.visible) {
            if (ChatPanel.resizeContains(mx, my, s)) {
                dragMode = DRAG_CHAT_RESIZE;
                rsW = ChatPanel.pw;
                rsH = ChatPanel.ph;
                rsMX = mx;
                rsMY = my;
                rsScale = s;
                return;
            }
            if (ChatPanel.titleContains(mx, my, s)) {
                dragMode = DRAG_CHAT_MOVE;
                dragDX = mx / s - ChatPanel.px;
                dragDY = my / s - ChatPanel.py;
                return;
            }
            if (ChatPanel.inputContains(mx, my, s)) {
                OverlayInput.setFocused(true);
                return;
            }
            if (ChatPanel.bodyContains(mx, my, s)) {
                OverlayInput.setFocused(false);
                return;
            }
        }
        OverlayInput.setFocused(false); // 点空白：失焦
    }

    /** 主栏按钮命中与执行；返回 true = 消费了这次点击。 */
    private static boolean mainBarPress(l engine, int mx, int my, float s) {
        if (!AiState.panelVisible) {
            if (Ui.hit(BX * s, BY * s, MINI_W * s, MINI_H * s, mx, my)) {
                AiState.panelVisible = true;
                Log.info("overlay button: panel on");
                AiState.notifyChange();
                return true;
            }
            return false;
        }
        for (int i = 0; i < BTN_COUNT; i++) {
            float x = (BX + i * (BW + BGAP)) * s;
            if (!Ui.hit(x, BY * s, BW * s, BH * s, mx, my)) {
                continue;
            }
            switch (i) {
                case 0:
                    AiState.enabled = !AiState.enabled;
                    Log.info("overlay button: AI " + (AiState.enabled ? "on" : "off"));
                    AiState.notifyChange();
                    break;
                case 1:
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
                    AiState.notifyChange();
                    break;
                case 2:
                    ChatPanel.visible = !ChatPanel.visible;
                    if (!ChatPanel.visible) {
                        OverlayInput.setFocused(false); // 防看不见的输入框吞键
                    }
                    Log.info("overlay button: chat " + (ChatPanel.visible ? "on" : "off"));
                    break;
                case 3:
                    DelegPanel.visible = !DelegPanel.visible;
                    if (DelegPanel.visible) {
                        DelegPanel.invalidate(); // 打开时立即刷新快照
                    }
                    Log.info("overlay button: deleg " + (DelegPanel.visible ? "on" : "off"));
                    break;
                case 4:
                    AgentPanel.visible = !AgentPanel.visible;
                    Log.info("overlay button: agent settings " + (AgentPanel.visible ? "on" : "off"));
                    break;
                case 5:
                    AiState.panelVisible = false;
                    OverlayInput.setFocused(false);
                    Log.info("overlay button: panel off");
                    AiState.notifyChange();
                    break;
                default:
                    break;
            }
            return true;
        }
        return false;
    }

    /** 按住拖动中：更新面板位置/尺寸。 */
    private static void drag(l engine, int mx, int my) {
        if (engine == null) {
            return;
        }
        float s = engine.cj; // cj=UI 密度；cX 是相机缩放（禁用）
        switch (dragMode) {
            case DRAG_CHAT_MOVE:
                ChatPanel.moveTo(mx / s - dragDX, my / s - dragDY);
                break;
            case DRAG_DELEG_MOVE:
                DelegPanel.moveTo(mx / s - dragDX, my / s - dragDY);
                break;
            case DRAG_AGENT_MOVE:
                AgentPanel.moveTo(mx / s - dragDX, my / s - dragDY);
                break;
            case DRAG_CHAT_RESIZE:
                ChatPanel.resizeTo(rsW + (mx - rsMX) / rsScale, rsH + (my - rsMY) / rsScale);
                break;
            default:
                break;
        }
    }

    // ---------- 绘制（渲染线程） ----------

    static void draw() {
        l engine = l.B();
        if (engine == null || engine.bO == null || engine.bs == null || engine.bs.w) {
            return; // 菜单/AI 混战不显示
        }
        Ui.ensure();
        float scale = engine.cj; // cj=UI 密度；cX 是相机缩放（禁用：面板会跟地图变焦）
        Ui.applyScale(scale);
        y g = engine.bO;
        g.k();
        engine.S(); // 撤销相机缩放，画屏固尺寸（游戏自带血条同款模式）
        try {
            drawMainBar(g, scale);
            if (AiState.panelVisible) {
                drawStatusLines(g, scale);
                DelegPanel.draw(g, scale);
                AgentPanel.draw(g, scale);
                ChatPanel.draw(g, scale);
            }
        } finally {
            g.l();
        }
    }

    private static void drawMainBar(y g, float scale) {
        try {
            if (!AiState.panelVisible) { // 只留恢复用小按钮
                Ui.button(g, BX * scale, BY * scale, MINI_W * scale, MINI_H * scale, "面板:关", false);
                return;
            }
            String[] labels = new String[BTN_COUNT];
            boolean[] on = new boolean[BTN_COUNT];
            labels[0] = AiState.enabled ? "AI:开" : "AI:关";
            on[0] = AiState.enabled;
            labels[1] = "节拍:" + (AiState.intervalOverride <= 0 ? "自动" : ((int) AiState.intervalOverride + "s"));
            on[1] = AiState.intervalOverride > 0;
            labels[2] = "聊天";
            on[2] = ChatPanel.visible;
            labels[3] = "委托";
            on[3] = DelegPanel.visible;
            labels[4] = "设置";
            on[4] = AgentPanel.visible;
            labels[5] = "面板:开";
            on[5] = true;
            for (int i = 0; i < BTN_COUNT; i++) {
                Ui.button(g, (BX + i * (BW + BGAP)) * scale, BY * scale, BW * scale, BH * scale,
                    labels[i], on[i]);
            }
        } catch (Throwable ignored) {
        }
    }

    /** bridge text 协议的状态行，画在主栏下方（上限 8 行）。 */
    private static void drawStatusLines(y g, float scale) {
        try {
            String[] ls = lines.get();
            if (ls.length == 0) {
                return;
            }
            float x = BX * scale;
            float yy = (BY + BH + 12) * scale;
            int step = (int) Ui.text.k() + 14;
            int shown = 0;
            for (String str : ls) {
                if (str == null || str.isEmpty()) {
                    continue;
                }
                // 实测：第一个 Paint=文字色，第二个=底板色
                g.a(str, x, yy, Ui.text, Ui.statusBg, 6.0f);
                yy += step;
                if (++shown >= 8) {
                    break;
                }
            }
        } catch (Throwable ignored) {
        }
    }
}
