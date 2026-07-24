package rw.agent;

import com.corrodinggames.rts.game.n;
import com.corrodinggames.rts.game.units.am;
import com.corrodinggames.rts.gameFramework.l;
import com.corrodinggames.rts.gameFramework.utility.o;

/**
 * Builds one NDJSON snapshot line of the current game state.
 * Fairness rule: enemy units are included ONLY if currently visible to the
 * local team (u.d(team) fog check) — no wallhack. Ally units are included in
 * full (team games share vision/intel anyway). Called on the game thread.
 */
public final class Snapshot {
    private Snapshot() {
    }

    // ---- income/expense plumbing（custom.e.a 编译期不可达：类 custom.e 与包
    // custom.e/ 同名冲突，javac 无法解析，与 Commands 的 action-id 同款问题） ----
    private static Object creditsAction;            // 常量 custom.e.a.D（credits 动作）
    private static java.lang.reflect.Method teamIncome;   // n.b(e.a) 收入速率
    private static java.lang.reflect.Method teamExpense;  // n.a(e.a) 支出速率
    private static int lastFogTick = -1;            // 迷雾附件上次发送 tick（-1=未发过）

    private static synchronized boolean ensureRateReflect() {
        if (creditsAction != null) {
            return true;
        }
        try {
            Class<?> actionClass = Class.forName("com.corrodinggames.rts.game.units.custom.e.a");
            creditsAction = actionClass.getField("D").get(null);
            teamIncome = n.class.getMethod("b", actionClass);
            teamExpense = n.class.getMethod("a", actionClass);
            Log.info("income/expense reflection ok");
            return true;
        } catch (Throwable t) {
            Log.error("income/expense reflection failed", t);
            return false;
        }
    }

    public static String build() {
        l engine = l.B();
        n team = engine.bs;
        StringBuilder sb = new StringBuilder(8192);
        sb.append("{\"type\":\"snapshot\",\"tick\":").append(engine.bx);
        sb.append(",\"timeMs\":").append(engine.by);
        if (engine.bL != null) {
            sb.append(",\"map\":{\"tilesW\":").append(engine.bL.C)
              .append(",\"tilesH\":").append(engine.bL.D).append('}');
        }
        if (team != null) {
            sb.append(",\"team\":{\"id\":").append(team.k)
              .append(",\"funds\":").append((long) team.o);
            try {
                // 队伍收支速率（游戏 UI "(+26)" 同款数据源：n.b/a(credits)）
                if (ensureRateReflect()) {
                    sb.append(",\"income\":").append(teamIncome.invoke(team, creditsAction))
                      .append(",\"expense\":").append(teamExpense.invoke(team, creditsAction));
                }
            } catch (Throwable ignored) {
            }
            sb.append(",\"ai\":").append(team.w).append('}');
            sb.append(",\"delegated\":[");
            java.util.ArrayList<am> del = Commands.delegatedUnits(team);
            for (int i = 0; i < del.size(); i++) {
                if (i > 0) {
                    sb.append(',');
                }
                sb.append(del.get(i).eh);
            }
            sb.append(']');
            sb.append(",\"groups\":").append(Commands.groupsSummary(team));
        }
        int ownCount = 0;
        int enemyCount = 0;
        int allyCount = 0;
        StringBuilder own = new StringBuilder(2048);
        StringBuilder enemy = new StringBuilder(2048);
        StringBuilder ally = new StringBuilder(2048);
        o units = am.bF();
        Object[] arr = units.b();
        int size = units.size();
        for (int i = 0; i < size; i++) {
            try {
                Object obj = arr[i];
                if (!(obj instanceof am)) {
                    continue;
                }
                am u = (am) obj;
                if (u.bV || u.ej) {
                    continue;
                }
                if (team == null) {
                    break;
                }
                if (u.bX == team) {
                    if (ownCount++ > 0) {
                        own.append(',');
                    }
                    // act 覆盖全部我方单位（执行层需要全体建造者的忙闲，而非仅委托单位）
                    appendUnit(own, u, false, Activity.describe(u));
                } else if (team.c(u.bX)) {
                    if (!u.d(team)) {
                        continue; // enemy in fog — not included, no wallhack
                    }
                    if (enemyCount++ > 0) {
                        enemy.append(',');
                    }
                    appendUnit(enemy, u, true, null);
                } else if (team.d(u.bX)) {
                    if (allyCount++ > 0) {
                        ally.append(',');
                    }
                    appendUnit(ally, u, true, null);
                }
            } catch (Throwable ignored) {
                // one weird unit must not kill the snapshot
            }
        }
        sb.append(",\"ownCount\":").append(ownCount).append(",\"own\":[").append(own).append(']');
        sb.append(",\"allyCount\":").append(allyCount).append(",\"allies\":[").append(ally).append(']');
        sb.append(",\"enemyCount\":").append(enemyCount).append(",\"enemy\":[").append(enemy).append(']');
        // 迷雾附件 ~600 tick 一跳：不能用 bx%600==0（快照按墙钟 0.5s 采样，tick 相位
        // 不对齐就永远不命中）；换图 bx 归零要当作立即重发
        if (team != null && (lastFogTick < 0 || engine.bx < lastFogTick
                             || engine.bx - lastFogTick >= 600)) {
            lastFogTick = engine.bx;
            String fog = fogSectors(team);
            if (fog != null) {
                sb.append(",\"fog\":").append(fog);
            }
            String fogGrid = fogGridRle(engine, team);
            if (fogGrid != null) {
                sb.append(",\"fogGrid\":").append(fogGrid);
            }
            String ghosts = ghostBuildings(team);
            if (ghosts != null) {
                sb.append(",\"ghosts\":").append(ghosts);
            }
        }
        sb.append('}');
        return sb.toString();
    }

    /** 3x3 区域探索度（本队迷雾网格 N：10=未探索）。每 10 秒随快照发一次。 */
    private static String fogSectors(n team) {
        byte[][] grid = team.N;
        if (grid == null) {
            return null;
        }
        int w = team.L;
        int h = team.M;
        if (w <= 0 || h <= 0) {
            return null;
        }
        String[] names = {"NW", "N", "NE", "W", "C", "E", "SW", "S", "SE"};
        StringBuilder sb = new StringBuilder("{");
        for (int sy = 0; sy < 3; sy++) {
            for (int sx = 0; sx < 3; sx++) {
                int x0 = sx * w / 3;
                int x1 = (sx + 1) * w / 3;
                int y0 = sy * h / 3;
                int y1 = (sy + 1) * h / 3;
                int total = 0;
                int explored = 0;
                for (int x = x0; x < x1; x++) {
                    for (int y = y0; y < y1; y++) {
                        total++;
                        if (grid[x][y] != 10) {
                            explored++;
                        }
                    }
                }
                if (sx + sy > 0) {
                    sb.append(',');
                }
                double frac = total == 0 ? 0 : (double) explored / total;
                sb.append("\"").append(names[sy * 3 + sx]).append("\":")
                  .append(String.format(java.util.Locale.ROOT, "%.2f", frac));
            }
        }
        return sb.append('}').toString();
    }

    /**
     * 三态迷雾网格（每格 0=当前可见 / 1=浅黑（探索过被回盖） / 2=全黑（未探索）），
     * 按行 RLE（"值x游程;"）。附迷雾模式 e=启用 f=LOS回盖 g=全图揭开。
     * 底层 team.N：&lt;5 可见 / 5..9 浅黑 / 10 全黑；无迷雾模式 N=null 不下发。
     */
    private static String fogGridRle(l engine, n team) {
        byte[][] grid = team.N;
        if (grid == null) {
            // 无迷雾/全图揭开（N=null）：仍下发模式，面板据此显示"无迷雾"而非"数据未同步"
            if (engine.bL == null) {
                return null;
            }
            return "{\"e\":" + engine.bL.E + ",\"f\":" + engine.bL.F + ",\"g\":" + engine.bL.G
                   + ",\"rows\":[]}";
        }
        int w = team.L;
        int h = team.M;
        if (w <= 0 || h <= 0) {
            return null;
        }
        StringBuilder sb = new StringBuilder(w * 2);
        sb.append("{\"w\":").append(w).append(",\"h\":").append(h);
        if (engine.bL != null) {
            sb.append(",\"e\":").append(engine.bL.E)
              .append(",\"f\":").append(engine.bL.F)
              .append(",\"g\":").append(engine.bL.G);
        }
        sb.append(",\"rows\":[");
        for (int y = 0; y < h; y++) {
            if (y > 0) {
                sb.append(',');
            }
            sb.append('"');
            int runVal = -1;
            int runLen = 0;
            for (int x = 0; x < w; x++) {
                int v = grid[x][y];
                int t = v < 5 ? 0 : (v < 10 ? 1 : 2);
                if (t == runVal) {
                    runLen++;
                    continue;
                }
                if (runVal >= 0) {
                    sb.append(runVal).append('x').append(runLen).append(';');
                }
                runVal = t;
                runLen = 1;
            }
            sb.append(runVal).append('x').append(runLen);
            sb.append('"');
        }
        return sb.append(']').append('}').toString();
    }

    // ---- 浅黑记忆建筑（幽灵注册表 gameFramework.d.a.w：游戏自维护，=玩家在浅黑区所见，公平） ----
    private static java.lang.reflect.Field fGhostReg;   // d.a.w（静态 o 列表）
    private static java.lang.reflect.Field fGType;      // d.a.d（as 类型，.i() 名字）
    private static java.lang.reflect.Field fGOwner;     // d.a.e（原属队伍 n）
    private static java.lang.reflect.Field fGX;         // d.a.g（世界坐标 x）
    private static java.lang.reflect.Field fGY;         // d.a.h（世界坐标 y）
    private static java.lang.reflect.Field fGObserver;  // d.a.j（观察方 n，只取本队）
    private static java.lang.reflect.Field fGBlueprint; // d.a.n（true=建造蓝图预览，过滤）
    private static java.lang.reflect.Field fGPurge;     // d.a.c（true=待移除，过滤）
    private static boolean ghostInit = false;

    private static synchronized boolean ensureGhostReflect() {
        if (ghostInit) {
            return fGhostReg != null;
        }
        ghostInit = true;
        try {
            Class<?> g = Class.forName("com.corrodinggames.rts.gameFramework.d.a");
            fGhostReg = g.getField("w");
            fGType = g.getField("d");
            fGOwner = g.getField("e");
            fGX = g.getField("g");
            fGY = g.getField("h");
            fGObserver = g.getField("j");
            fGBlueprint = g.getField("n");
            fGPurge = g.getField("c");
            Log.info("ghost reflection ok");
        } catch (Throwable t) {
            fGhostReg = null;
            Log.error("ghost reflection failed", t);
        }
        return fGhostReg != null;
    }

    /** 浅黑区记忆建筑清单（本队视角幽灵：[{t,x,y,team}]）。 */
    private static String ghostBuildings(n team) {
        if (!ensureGhostReflect()) {
            return null;
        }
        try {
            o reg = (o) fGhostReg.get(null);
            if (reg == null) {
                return null;
            }
            Object[] arr = reg.b();
            int size = reg.size();
            StringBuilder sb = new StringBuilder(256);
            int count = 0;
            for (int i = 0; i < size; i++) {
                Object g = arr[i];
                if (g == null) {
                    continue;
                }
                try {
                    if (fGObserver.get(g) != team) {
                        continue;
                    }
                    if (fGPurge.getBoolean(g) || fGBlueprint.getBoolean(g)) {
                        continue;
                    }
                    Object type = fGType.get(g);
                    String name = (type == null) ? "?"
                            : ((com.corrodinggames.rts.game.units.as) type).i();
                    Object owner = fGOwner.get(g);
                    int ownerId = (owner == null) ? -1 : ((n) owner).k;
                    if (count++ > 0) {
                        sb.append(',');
                    }
                    sb.append("{\"t\":\"").append(esc(name)).append('"')
                      .append(",\"x\":").append((int) fGX.getFloat(g))
                      .append(",\"y\":").append((int) fGY.getFloat(g))
                      .append(",\"team\":").append(ownerId).append('}');
                } catch (Throwable ignored) {
                    // one weird ghost must not kill the snapshot
                }
            }
            return "[" + sb + "]";
        } catch (Throwable t) {
            return null;
        }
    }

    private static void appendUnit(StringBuilder sb, am u, boolean withTeam, String act) {
        sb.append("{\"id\":").append(u.eh)
          .append(",\"t\":\"").append(esc(u.r().i())).append('"')
          .append(",\"x\":").append((int) u.eo)
          .append(",\"y\":").append((int) u.ep)
          .append(",\"hp\":").append((int) u.cu)
          .append(",\"maxHp\":").append((int) u.cv);
        try {
            if (u.r().j()) {
                sb.append(",\"bld\":true");
                Integer q = queueDepth(u);
                if (q != null) {
                    sb.append(",\"q\":").append(q.intValue());
                }
            }
        } catch (Throwable ignored) {
        }
        if (withTeam) {
            sb.append(",\"team\":").append(u.bX.k);
        }
        if (!withTeam && u.cm < 1.0f) {
            // 自家中正在建造的建筑：完成度 0~1
            sb.append(",\"prog\":").append(String.format(java.util.Locale.ROOT, "%.2f", u.cm));
        }
        if (act != null) {
            sb.append(",\"act\":\"").append(esc(act)).append('"');
        }
        sb.append('}');
    }

    private static java.lang.reflect.Field fQueueMgr;   // units.d.i.z（队列管理器）
    private static java.lang.reflect.Field fQueue;      // units.d.k.c（主生产队列）
    private static boolean queueInit = false;

    /** 建筑的生产/升级队列长度；非建筑或无队列返回 null。 */
    private static Integer queueDepth(am u) {
        if (!queueInit) {
            queueInit = true;
            try {
                fQueueMgr = Class.forName("com.corrodinggames.rts.game.units.d.i").getDeclaredField("z");
                fQueueMgr.setAccessible(true);
                fQueue = Class.forName("com.corrodinggames.rts.game.units.d.k").getField("c");
            } catch (Throwable t) {
                fQueueMgr = null;
            }
        }
        if (fQueueMgr == null || fQueue == null) {
            return null;
        }
        try {
            Object k = fQueueMgr.get(u);
            if (k == null) {
                return null;
            }
            Object m = fQueue.get(k);
            if (m == null) {
                return null;
            }
            return Integer.valueOf(((com.corrodinggames.rts.gameFramework.utility.m) m).size());
        } catch (Throwable t) {
            return null;
        }
    }

    private static String esc(String s) {
        if (s == null) {
            return "?";
        }
        StringBuilder sb = new StringBuilder(s.length() + 8);
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            if (c == '"' || c == '\\') {
                sb.append('\\').append(c);
            } else if (c < 32) {
                sb.append('?');
            } else {
                sb.append(c);
            }
        }
        return sb.toString();
    }
}
