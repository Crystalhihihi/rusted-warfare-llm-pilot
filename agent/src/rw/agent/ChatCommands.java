package rw.agent;

import java.lang.reflect.Field;
import java.util.HashSet;
import java.util.concurrent.ConcurrentLinkedQueue;

import com.corrodinggames.rts.gameFramework.l;

/**
 * 聊天框命令：每 tick 轮询游戏聊天记录（ad.aC 队列，45 条环形缓冲），
 * 消费 "/ai" 开头的消息并回显确认。零插桩——只读公开队列。
 *
 * 支持：/ai  /ai on|off  /ai fast|mid|calm|auto  /ai interval N
 *       /ai groups 0,3,9（用户编队号）  /ai panel
 */
public final class ChatCommands {
    private static boolean init = false;
    private static Field fText;
    private static Field fTime;
    private static long lastTime = -1; // -1 = 首次（跳过历史消息）

    private ChatCommands() {
    }

    private static synchronized boolean ensure() {
        if (init) {
            return fText != null;
        }
        init = true;
        try {
            Class<?> entry = Class.forName("com.corrodinggames.rts.gameFramework.j.b");
            fText = entry.getDeclaredField("c");
            fText.setAccessible(true);
            fTime = entry.getDeclaredField("e");
            fTime.setAccessible(true);
            Log.info("chat-command reflection ok");
        } catch (Throwable t) {
            fText = null;
            Log.error("chat-command reflection failed", t);
        }
        return fText != null;
    }

    /** Game thread only (called from TickLoop). */
    public static void tick() {
        if (!ensure()) {
            return;
        }
        try {
            Object ad = l.B().bX;
            if (ad == null) {
                return;
            }
            Object log = ad.getClass().getField("aC").get(ad);
            if (log == null) {
                return;
            }
            ConcurrentLinkedQueue<?> q = (ConcurrentLinkedQueue<?>) log.getClass().getMethod("b").invoke(log);
            long newest = lastTime;
            for (Object entry : q) {
                long t = fTime.getLong(entry);
                if (lastTime >= 0 && t <= lastTime) {
                    continue;
                }
                if (t > newest) {
                    newest = t;
                }
                if (lastTime < 0) {
                    continue; // 首次运行只建立基线，不消费历史
                }
                String text = (String) fText.get(entry);
                if (text != null) {
                    handle(text.trim());
                }
            }
            lastTime = newest;
        } catch (Throwable t) {
            Log.error("chatcmd tick error", t);
        }
    }

    private static void handle(String text) {
        // "/s 内容"：玩家直接给 AI 留言，转发桥接，进下一轮 LLM 输入
        if (text.startsWith("/s ")) {
            String msg = text.substring(3).trim();
            if (!msg.isEmpty()) {
                NetServer.send("{\"type\":\"userMsg\",\"text\":\"" + Commands.esc(msg) + "\"}");
                echo("已转告AI: " + msg);
            }
            return;
        }
        if (!text.startsWith("/ai")) {
            return;
        }
        Log.info("chat command: " + text);
        String[] parts = text.split("\\s+");
        if (parts.length == 1) {
            echo(AiState.statusText());
            return;
        }
        String sub = parts[1].toLowerCase();
        try {
            switch (sub) {
                case "on":
                    AiState.enabled = true;
                    echo("已启动");
                    break;
                case "off":
                    AiState.enabled = false;
                    echo("已停止（零API消耗）");
                    break;
                case "fast":
                    AiState.intervalOverride = 2.5f;
                    echo("节拍=2.5s");
                    break;
                case "mid":
                    AiState.intervalOverride = 6f;
                    echo("节拍=6s");
                    break;
                case "calm":
                    AiState.intervalOverride = 12f;
                    echo("节拍=12s");
                    break;
                case "auto":
                    AiState.intervalOverride = 0f;
                    echo("节拍=自动");
                    break;
                case "interval":
                    if (parts.length < 3) {
                        echo("用法: /ai interval 秒");
                        return;
                    }
                    float n = Float.parseFloat(parts[2]);
                    n = Math.max(1f, Math.min(60f, n));
                    AiState.intervalOverride = n;
                    echo("节拍=" + (int) n + "s");
                    break;
                case "groups":
                    if (parts.length < 3) {
                        echo("用法: /ai groups 0,3,9");
                        return;
                    }
                    applyGroups(parts[2]);
                    break;
                case "panel":
                    AiState.panelVisible = !AiState.panelVisible;
                    echo("面板" + (AiState.panelVisible ? "显示" : "隐藏"));
                    break;
                default:
                    echo("未知命令 " + sub + "；/ai 查看状态，可用: on/off/fast/mid/calm/auto/interval N/groups 0,9/panel");
                    return;
            }
            AiState.notifyChange();
        } catch (Throwable t) {
            echo("命令出错: " + t.getMessage());
        }
    }

    private static void applyGroups(String csv) {
        HashSet<Integer> slots = new HashSet<Integer>();
        for (String s : csv.split(",")) {
            s = s.trim();
            if (s.isEmpty()) {
                continue;
            }
            int g = Integer.parseInt(s);
            if (g < 0 || g > 9) {
                echo("编队号超出范围: " + g);
                return;
            }
            slots.add(g == 0 ? 9 : (g - 1) % 10); // 用户编队号 → 内部槽位
        }
        Commands.setDelegatedSlots(slots);
        echo("委托编队=" + Commands.delegatedSlotsString());
    }

    private static void echo(String msg) {
        try {
            l.B().bX.m("[AI] " + msg);
        } catch (Throwable t) {
            Log.error("chat echo failed", t);
        }
    }
}
