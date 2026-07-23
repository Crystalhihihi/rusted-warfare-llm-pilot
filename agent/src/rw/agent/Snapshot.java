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
                sb.append(",\"income\":").append(team.b(com.corrodinggames.rts.game.units.custom.e.a.D))
                  .append(",\"expense\":").append(team.a(com.corrodinggames.rts.game.units.custom.e.a.D));
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
        if (engine.bx % 600 == 0 && team != null) {
            String fog = fogSectors(team);
            if (fog != null) {
                sb.append(",\"fog\":").append(fog);
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
