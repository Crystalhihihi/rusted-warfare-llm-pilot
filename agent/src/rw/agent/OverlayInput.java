package rw.agent;

/**
 * 聊天输入框的键盘/滚轮胶水（Hook 版）。
 * Hooks transformer 在 com.corrodinggames.rts.java.u 的
 * keyPressed(int,char)/keyReleased(int,char)/mouseWheelMoved(int) 方法头插入
 * 对本类的回调；返回 true = 事件被我们吃掉（游戏快捷键同时失效——与游戏自带
 * 聊天框经 librocket 抑制快捷键的路径逐字节一致：键根本不进游戏键态数组）。
 *
 * 字符直接来自 Slick 翻译好的 char（布局/Shift/key-repeat 都已处理）。
 * 约束（证据见调查）：
 *  - 反向让行：游戏自己的 libRocket 弹窗（聊天/菜单）持焦时（a.j()==true）一律放行
 *  - 所有回调绝不抛异常、默认放行：hook 失败最坏结果只是输入框失效，游戏按键不受影响
 *  - 回调跑在游戏主线程（Slick 事件回调），渲染线程经 volatile/快照读
 *  - LWJGL2 常量：KEY_ESCAPE=1 KEY_BACK=14 KEY_RETURN=28 KEY_NUMPADENTER=156
 */
public final class OverlayInput {
    private static volatile boolean focused = false;
    private static final StringBuilder text = new StringBuilder(64);
    private static volatile long lastEditTs = 0L;
    private static final int MAX_LEN = 120;

    private OverlayInput() {
    }

    // ---------- ChatPanel/OverlayData 用的公开 API ----------

    public static boolean isFocused() {
        return focused;
    }

    public static void setFocused(boolean f) {
        if (focused != f) {
            focused = f;
            Log.info("overlay input focus=" + f);
        }
    }

    /** 渲染线程读：当前输入内容快照。 */
    public static String text() {
        synchronized (text) {
            return text.toString();
        }
    }

    public static void clearText() {
        synchronized (text) {
            text.setLength(0);
        }
    }

    /** 光标闪烁相位（秒级单调时钟即可）。 */
    public static boolean caretOn() {
        return (System.currentTimeMillis() / 500) % 2 == 0 || System.currentTimeMillis() - lastEditTs < 500;
    }

    // ---------- Hook 回调（transformer 注入点；必须绝不出错） ----------

    /** keyPressed 注入。返回 true=吞掉该事件。 */
    public static boolean onKeyPressed(int key, char c) {
        try {
            if (!focused || gamePopupFocused()) {
                return false;
            }
            if (key == 28 || key == 156) {          // Enter：发送
                String t = text().trim();
                if (!t.isEmpty()) {
                    NetServer.send("{\"type\":\"userMsg\",\"text\":\"" + Commands.esc(t) + "\"}");
                    OverlayData.addChat("player", t);
                    Log.info("overlay chat sent: " + t);
                }
                clearText();
                return true;
            }
            if (key == 1) {                          // Esc：失焦
                setFocused(false);
                return true;
            }
            if (key == 14) {                         // Backspace
                synchronized (text) {
                    if (text.length() > 0) {
                        text.setLength(text.length() - 1);
                        lastEditTs = System.currentTimeMillis();
                    }
                }
                return true;
            }
            if (c >= 32 && c != 127) {               // 可打印字符（仅 ASCII/拉丁；中文 IME 不可得，与游戏一致）
                synchronized (text) {
                    if (text.length() < MAX_LEN) {
                        text.append(c);
                        lastEditTs = System.currentTimeMillis();
                    }
                }
                return true;
            }
            return true;                             // 聚焦期间其余键（方向键/F键等）一律吞，防触发游戏快捷键
        } catch (Throwable t) {
            return false;                            // 出错一律放行，绝不弄坏游戏按键
        }
    }

    /** keyReleased 注入。聚焦时同样吞掉（防按下时被吞、松开时进游戏键态的残留）。 */
    public static boolean onKeyReleased(int key, char c) {
        try {
            return focused && !gamePopupFocused();
        } catch (Throwable t) {
            return false;
        }
    }

    /** mouseWheelMoved 注入。光标在聊天滚动区上时吞掉（防地图同时缩放）。 */
    public static boolean onMouseWheel(int delta) {
        try {
            if (gamePopupFocused()) {
                return false;
            }
            int mx = org.lwjgl.input.Mouse.getX();
            int my = org.lwjgl.opengl.Display.getHeight() - org.lwjgl.input.Mouse.getY();
            if (OverlayData.chatScrollAreaContains(mx, my)) {
                OverlayData.chatScrollBy(delta / 120);   // 滚轮上(delta>0)=回看历史，与系统滚动方向一致
                return true;
            }
            return false;
        } catch (Throwable t) {
            return false;
        }
    }

    /** 游戏自己的 libRocket 弹窗（聊天/菜单）是否持焦：是则我们让行。 */
    private static boolean gamePopupFocused() {
        try {
            return com.corrodinggames.librocket.a.a() != null && com.corrodinggames.librocket.a.a().j();
        } catch (Throwable t) {
            return false;
        }
    }
}
