package rw.agent;

/**
 * AI 控制的共享状态（游戏线程内修改，渲染线程只读）。
 * 变更后通过 notifyChange() 广播给 bridge（pilot 据此启停 LLM 调用）。
 */
public final class AiState {
    public static volatile boolean enabled = true;
    public static volatile boolean panelVisible = true;
    /** 节拍覆盖（秒），0 = 自动（自适应） */
    public static volatile float intervalOverride = 0f;
    /** 角色开关（设置面板）：总指挥/建造/战斗，默认全开 */
    public static volatile boolean cmdCommander = true;
    public static volatile boolean cmdBuild = true;
    public static volatile boolean cmdCombat = true;
    /** 委托单位 id 池（游戏内[委托选中]按钮/框选添加） */
    public static final java.util.Set<Long> delegatedIds =
        java.util.Collections.synchronizedSet(new java.util.HashSet<Long>());
    /** 全队接管模式：true 时忽略委托池，控制本队全部单位 */
    public static volatile boolean controlAll = false;

    private AiState() {
    }

    public static void notifyChange() {
        int n;
        synchronized (delegatedIds) {
            n = delegatedIds.size();
        }
        NetServer.send("{\"type\":\"aiControl\",\"enabled\":" + enabled
            + ",\"panel\":" + panelVisible
            + ",\"interval\":" + intervalOverride
            + ",\"controlAll\":" + controlAll
            + ",\"cmdCommander\":" + cmdCommander
            + ",\"cmdBuild\":" + cmdBuild
            + ",\"cmdCombat\":" + cmdCombat
            + ",\"delegatedCount\":" + n
            + ",\"groups\":\"" + Commands.delegatedSlotsString() + "\"}");
    }

    public static String statusText() {
        int n;
        synchronized (delegatedIds) {
            n = delegatedIds.size();
        }
        return (enabled ? "运行中" : "已停止")
            + "，面板" + (panelVisible ? "开" : "关")
            + "，节拍=" + (intervalOverride <= 0 ? "自动" : ((int) intervalOverride + "s"))
            + "，委托=" + (controlAll ? "全队" : (n + "个单位/编队" + Commands.delegatedSlotsString()));
    }
}
