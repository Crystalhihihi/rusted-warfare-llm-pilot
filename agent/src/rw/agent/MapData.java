package rw.agent;

import java.lang.reflect.Field;
import java.lang.reflect.Method;
import java.util.ArrayList;

import com.corrodinggames.rts.gameFramework.l;

/**
 * One-shot per-match map description, sent to the bridge as a "map" message:
 * tile dimensions, world/tile scale, resource point world coords, and an
 * ASCII terrain grid (one char per tile).
 *
 * NOTE: everything touching the map class (game.b.b) and tile class (game.b.g)
 * goes through reflection, because the jar contains BOTH a class named
 * game.b and a package game.b/ — javac cannot resolve the type names.
 * (Field access like engine.bL.C still compiles; method calls cannot.)
 *
 * Tile flags (b.g): e=water f=bridge g=lava h=cliff k=largeCliff.
 * Game thread only; runs once per match, so reflection cost is irrelevant.
 */
public final class MapData {
    private MapData() {
    }

    public static String build() {
        l engine = l.B();
        Object map = engine.bL;
        if (map == null) {
            return null;
        }
        try {
            Class<?> mc = map.getClass();
            int w = mc.getField("C").getInt(map);
            int h = mc.getField("D").getInt(map);
            float tsx = 1.0f / mc.getField("r").getFloat(map);
            float tsy = 1.0f / mc.getField("s").getFloat(map);

            StringBuilder sb = new StringBuilder(w * h + 4096);
            sb.append("{\"type\":\"map\",\"tilesW\":").append(w).append(",\"tilesH\":").append(h)
              .append(",\"tileW\":").append(tsx).append(",\"tileH\":").append(tsy);

            appendResources(sb, map, mc);
            sb.append(",\"legend\":{\".\":\"land\",\"~\":\"water\",\"=\":\"bridge\",\"L\":\"lava\",\"^\":\"cliff\",\"#\":\"largeCliff\",\" \":\"void\"}");

            Method tileAt = mc.getMethod("c", float.class, float.class);
            TileFlags tf = new TileFlags();
            sb.append(",\"rows\":[");
            for (int ty = 0; ty < h; ty++) {
                if (ty > 0) {
                    sb.append(',');
                }
                sb.append('"');
                for (int tx = 0; tx < w; tx++) {
                    Object t = tileAt.invoke(map, (tx + 0.5f) * tsx, (ty + 0.5f) * tsy);
                    sb.append(tf.ch(t));
                }
                sb.append('"');
            }
            sb.append(']');
            sb.append('}');
            return sb.toString();
        } catch (Throwable t) {
            Log.error("MapData.build failed", t);
            return null;
        }
    }

    private static void appendResources(StringBuilder sb, Object map, Class<?> mc) throws Exception {
        sb.append(",\"resources\":[");
        ArrayList<?> res = (ArrayList<?>) mc.getField("A").get(map);
        Method toWorld = null;
        Field fx = null;
        Field fy = null;
        Method tileAt = mc.getMethod("c", float.class, float.class);
        Field fWater = null;
        boolean first = true;
        if (res != null) {
            for (Object p : res) {
                if (p == null) {
                    continue;
                }
                if (toWorld == null) {
                    toWorld = mc.getMethod("a", p.getClass());
                }
                Object pf = toWorld.invoke(map, p);
                if (pf == null) {
                    continue;
                }
                if (fx == null) {
                    fx = pf.getClass().getField("a");
                    fy = pf.getClass().getField("b");
                }
                float px = fx.getFloat(pf);
                float py = fy.getFloat(pf);
                // 水上资源点标记（tile.e = water）
                boolean water = false;
                try {
                    Object tile = tileAt.invoke(map, px, py);
                    if (tile != null) {
                        if (fWater == null) {
                            fWater = tile.getClass().getField("e");
                        }
                        water = fWater.getBoolean(tile);
                    }
                } catch (Throwable ignored) {
                }
                if (!first) {
                    sb.append(',');
                }
                first = false;
                sb.append("{\"x\":").append((int) px)
                  .append(",\"y\":").append((int) py);
                if (water) {
                    sb.append(",\"w\":true");
                }
                sb.append('}');
            }
        }
        sb.append(']');
    }

    /** Caches tile flag fields after first use (all tiles share one class). */
    private static final class TileFlags {
        private Field k, g, h, f, e;

        char ch(Object t) throws Exception {
            if (t == null) {
                return ' ';
            }
            if (k == null) {
                Class<?> tc = t.getClass();
                k = tc.getField("k");
                g = tc.getField("g");
                h = tc.getField("h");
                f = tc.getField("f");
                e = tc.getField("e");
            }
            if (k.getBoolean(t)) {
                return '#'; // 高崖，仅空军可过
            }
            if (g.getBoolean(t)) {
                return 'L'; // 岩浆
            }
            if (h.getBoolean(t)) {
                return '^'; // 阶梯/悬崖，OVER_CLIFF 兵种可上
            }
            if (f.getBoolean(t)) {
                return '='; // 水面桥
            }
            if (e.getBoolean(t)) {
                return '~'; // 水
            }
            return '.';
        }
    }
}
