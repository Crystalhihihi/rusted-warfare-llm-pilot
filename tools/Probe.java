import com.corrodinggames.rts.game.units.ao;
import com.corrodinggames.rts.game.units.av;

/** One-off probe: print real runtime names of movement-type & waypoint-type enums. */
public class Probe {
    public static void main(String[] args) {
        System.out.println("-- ao (movement) --");
        for (ao v : ao.values()) {
            System.out.println("ord=" + v.ordinal() + " name=" + v.name());
        }
        System.out.println("-- av (waypoint type) --");
        for (av v : av.values()) {
            System.out.println("ord=" + v.ordinal() + " name=" + v.name());
        }
    }
}
