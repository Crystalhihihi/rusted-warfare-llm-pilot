package rw.agent;

import java.lang.reflect.Field;

import com.corrodinggames.rts.game.units.am;
import com.corrodinggames.rts.game.units.y;

/**
 * Describes a unit's current activity ("idle", "move@(x,y)", "attackMove@(x,y)",
 * "attack→tank", ...) from its current waypoint (y.ar()).
 * Waypoint fields (au.a type / au.e,f coords / au.h target) are package-private,
 * so they are read reflectively. Runtime waypoint type names are real
 * (move/attack/build/repair/attackMove/patrol/guard/... — probe-verified).
 */
public final class Activity {
    private static boolean init = false;
    private static Field fType;
    private static Field fX;
    private static Field fY;
    private static Field fTarget;

    private Activity() {
    }

    private static synchronized boolean ensure() {
        if (init) {
            return fType != null;
        }
        init = true;
        try {
            Class<?> au = Class.forName("com.corrodinggames.rts.game.units.au");
            fType = au.getDeclaredField("a");
            fType.setAccessible(true);
            fX = au.getDeclaredField("e");
            fX.setAccessible(true);
            fY = au.getDeclaredField("f");
            fY.setAccessible(true);
            fTarget = au.getDeclaredField("h");
            fTarget.setAccessible(true);
            Log.info("activity reflection ok");
        } catch (Throwable t) {
            fType = null;
            Log.error("activity reflection failed", t);
        }
        return fType != null;
    }

    /** Short activity string for a unit, or null if unavailable. Game thread only. */
    public static String describe(am u) {
        if (!(u instanceof y) || !ensure()) {
            return null;
        }
        try {
            Object wp = ((y) u).ar();
            if (wp == null) {
                return "idle";
            }
            Object type = fType.get(wp);
            String name = type == null ? "?" : ((Enum<?>) type).name();
            Object tgt = fTarget.get(wp);
            if (tgt instanceof am && !((am) tgt).ej) {
                return name + "→" + ((am) tgt).r().i();
            }
            float x = fX.getFloat(wp);
            float yv = fY.getFloat(wp);
            return name + "@(" + (int) x + "," + (int) yv + ")";
        } catch (Throwable t) {
            return null;
        }
    }
}
