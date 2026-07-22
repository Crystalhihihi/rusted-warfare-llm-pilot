package rw.agent;

import java.util.ArrayList;

import com.corrodinggames.rts.game.units.am;
import com.corrodinggames.rts.game.units.ar;
import com.corrodinggames.rts.game.units.as;
import com.corrodinggames.rts.game.units.y;

/**
 * One-shot per-match unit-pedia export: every registered unit type with
 * price / tech level / building & builder flags / movement type / hp /
 * weapon stats (target type, damage, dps, range, count) / buildable action ids.
 * Sent to the bridge as a "pedia" message.
 * Registry: ar.ae. Stats sample: shared instance am.a(as).
 * Game thread only.
 */
public final class Pedia {
    private static final int MAX_ENTRIES = 250;

    private Pedia() {
    }

    public static String build() {
        if (ar.ae == null) {
            return null;
        }
        StringBuilder sb = new StringBuilder(32768);
        sb.append("{\"type\":\"pedia\",\"units\":[");
        int count = 0;
        for (Object o : new ArrayList(ar.ae)) {
            try {
                if (!(o instanceof as)) {
                    continue;
                }
                as t = (as) o;
                if (count > 0) {
                    sb.append(',');
                }
                sb.append("{\"name\":\"").append(Commands.esc(t.i())).append('"')
                  .append(",\"display\":\"").append(Commands.esc(safeDisplay(t))).append('"')
                  .append(",\"price\":").append(t.c())
                  .append(",\"tech\":").append(t.g())
                  .append(",\"building\":").append(t.j())
                  .append(",\"builder\":").append(t.l())
                  .append(",\"move\":\"").append(t.o() == null ? "?" : t.o().name()).append('"');
                appendFootprint(sb, t);
                try {
                    am sample = am.a(t);
                    if (sample != null) {
                        sb.append(",\"hp\":").append((int) sample.cv);
                        appendAttack(sb, sample);
                    }
                } catch (Throwable ignored) {
                }
                sb.append(",\"builds\":[");
                boolean first = true;
                for (int tl = 1; tl <= 3; tl++) {
                    ArrayList acts;
                    try {
                        acts = t.a(tl);
                    } catch (Throwable ex) {
                        break;
                    }
                    if (acts == null) {
                        continue;
                    }
                    for (Object a : acts) {
                        // units.a.s 在编译期不可达（units.a 类与 units.a/ 包冲突），反射调 O()
                        String id;
                        try {
                            id = (String) a.getClass().getMethod("O").invoke(a);
                        } catch (Throwable ex) {
                            continue;
                        }
                        if (id == null || id.length() == 0 || id.charAt(0) == '<') {
                            continue;
                        }
                        if (!first) {
                            sb.append(',');
                        }
                        first = false;
                        sb.append("\"").append(Commands.esc(id)).append("\"");
                    }
                }
                sb.append(']');
                sb.append('}');
                count++;
                if (count >= MAX_ENTRIES) {
                    break;
                }
            } catch (Throwable ignored) {
            }
        }
        sb.append(']');
        sb.append('}');
        return sb.toString();
    }

    private static String safeDisplay(as t) {
        try {
            String d = t.e();
            return d == null ? "" : d;
        } catch (Throwable ex) {
            return "";
        }
    }

    /**
     * 建筑占地导出：constructionFootprint（custom.l.cY，放置校验同款 Rect，闭区间
     * 相对锚点格偏移），宽=c-a+1 格、高=d-b+1 格；非 custom 类型兜底 am.a(t).cd()。
     * 槽位网格按此为每种建筑分配空间。
     */
    private static void appendFootprint(StringBuilder sb, as t) {
        try {
            android.graphics.Rect r = null;
            if (t instanceof com.corrodinggames.rts.game.units.custom.l) {
                r = ((com.corrodinggames.rts.game.units.custom.l) t).cY;
            } else {
                am sample = am.a(t);
                if (sample != null) {
                    r = sample.cd();
                }
            }
            if (r == null) {
                return;
            }
            sb.append(",\"fp\":{\"w\":").append(r.c - r.a + 1)
              .append(",\"h\":").append(r.d - r.b + 1)
              .append(",\"ox\":").append(r.a)
              .append(",\"oy\":").append(r.b)
              .append('}');
        } catch (Throwable ignored) {
        }
    }

    /**
     * 武器属性导出：目标类型(g=对地/a=对空)/单轮总伤害 dmg/dps/最大射程 rng/门数 n。
     * 走实体统一接口 y（custom.j 与内置硬编码单位通用），无武装单位也发全 false
     * 的 atk，让 bridge 能区分"无武装"与"无数据"。硬编码单位未覆盖 q(int) 时
     * dmg/dps 为 0（官方口径需实弹试射，代价太大，见 ar.s()）。
     */
    private static void appendAttack(StringBuilder sb, am sample) {
        try {
            if (!(sample instanceof y)) {
                return;
            }
            y u = (y) sample;
            if (!u.l()) { // am.l() = canAttack
                sb.append(",\"atk\":{\"g\":false,\"a\":false,\"dmg\":0,\"dps\":0,\"rng\":0,\"n\":0}");
                return;
            }
            int n = Math.max(0, u.bl()); // 武器门数（战列舰等多炮塔 > 1）
            int dmg = 0;
            float dps = 0f;
            for (int i = 0; i < n; i++) {
                float d = u.q(i);  // 第 i 门单发伤害
                float cd = u.b(i); // 第 i 门冷却 tick（60 tick = 1s）
                dmg += (int) d;
                if (cd > 0) {
                    dps += d * 60.0f / cd;
                }
            }
            sb.append(",\"atk\":{\"g\":").append(u.ag())
              .append(",\"a\":").append(u.af())
              .append(",\"dmg\":").append(dmg)
              .append(",\"dps\":").append((int) (dps + 0.5f))
              .append(",\"rng\":").append((int) u.m())
              .append(",\"n\":").append(n)
              .append('}');
        } catch (Throwable ignored) {
        }
    }
}
