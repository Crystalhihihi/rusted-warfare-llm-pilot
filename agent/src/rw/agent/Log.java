package rw.agent;

import java.io.File;
import java.io.FileWriter;
import java.io.PrintWriter;
import java.text.SimpleDateFormat;
import java.util.Date;

/** Tiny synchronized file logger. Never throws. */
public final class Log {
    private static PrintWriter out;
    private static final SimpleDateFormat TS = new SimpleDateFormat("HH:mm:ss.SSS");

    private Log() {
    }

    public static synchronized void init(String path) {
        if (out != null) {
            return;
        }
        try {
            File f = new File(path);
            File parent = f.getAbsoluteFile().getParentFile();
            if (parent != null) {
                parent.mkdirs();
            }
            out = new PrintWriter(new FileWriter(f, true), true);
        } catch (Throwable t) {
            System.err.println("rw-agent: cannot open log " + path + ": " + t);
        }
    }

    public static void info(String msg) {
        write("INFO", msg, null);
    }

    public static void error(String msg, Throwable t) {
        write("ERROR", msg, t);
    }

    private static synchronized void write(String level, String msg, Throwable t) {
        String line = TS.format(new Date()) + " [" + level + "] [" + Thread.currentThread().getName() + "] " + msg;
        if (out != null) {
            out.println(line);
            if (t != null) {
                t.printStackTrace(out);
            }
        } else {
            System.err.println("rw-agent " + line);
            if (t != null) {
                t.printStackTrace();
            }
        }
    }
}
