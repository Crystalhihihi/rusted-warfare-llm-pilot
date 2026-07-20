package rw.agent;

import java.util.ArrayList;

import com.corrodinggames.rts.game.units.am;
import com.corrodinggames.rts.game.units.ar;
import com.corrodinggames.rts.game.units.as;

/**
 * One-shot per-match unit-pedia export: every registered unit type with
 * price / tech level / building & builder flags / movement type / hp /
 * buildable action ids. Sent to the bridge as a "pedia" message.
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
                try {
                    am sample = am.a(t);
                    if (sample != null) {
                        sb.append(",\"hp\":").append((int) sample.cv);
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
}
