package rw.agent;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.io.PrintWriter;
import java.net.InetAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.util.ArrayList;
import java.util.concurrent.ConcurrentLinkedQueue;

/**
 * Localhost NDJSON channel to the Python bridge. Multiple clients allowed
 * (main bridge + ad-hoc command senders): inbound lines from any client are
 * queued for the game thread; outbound lines are broadcast to all clients.
 * Socket I/O runs on dedicated threads; the game thread only touches queues
 * and non-blocking writes. Never throws into the game.
 */
public final class NetServer {
    private static final int PORT = 41234;
    private static final ConcurrentLinkedQueue<String> inbox = new ConcurrentLinkedQueue<String>();
    private static final ArrayList<Socket> clients = new ArrayList<Socket>();
    private static boolean started = false;

    private NetServer() {
    }

    public static synchronized void start() {
        if (started) {
            return;
        }
        started = true;
        Thread t = new Thread(new Runnable() {
            @Override
            public void run() {
                acceptLoop();
            }
        }, "rw-agent-net");
        t.setDaemon(true);
        t.start();
    }

    private static void acceptLoop() {
        ServerSocket server = null;
        try {
            server = new ServerSocket(PORT, 8, InetAddress.getByName("127.0.0.1"));
            Log.info("NetServer listening on 127.0.0.1:" + PORT);
        } catch (Throwable t) {
            Log.error("NetServer cannot bind port " + PORT, t);
            return;
        }
        while (true) {
            try {
                final Socket s = server.accept();
                s.setTcpNoDelay(true);
                synchronized (clients) {
                    clients.add(s);
                }
                Log.info("bridge connected: " + s.getRemoteSocketAddress() + " clients=" + clients.size());
                Thread reader = new Thread(new Runnable() {
                    @Override
                    public void run() {
                        readLoop(s);
                    }
                }, "rw-agent-net-reader");
                reader.setDaemon(true);
                reader.start();
            } catch (Throwable t) {
                Log.error("accept error", t);
                sleep(1000);
            }
        }
    }

    private static void readLoop(Socket s) {
        try {
            BufferedReader in = new BufferedReader(new InputStreamReader(s.getInputStream(), "UTF-8"));
            String line;
            while ((line = in.readLine()) != null) {
                inbox.offer(line);
            }
        } catch (Throwable ignored) {
            // client went away
        } finally {
            removeClient(s);
        }
    }

    private static void removeClient(Socket s) {
        synchronized (clients) {
            clients.remove(s);
            try {
                s.close();
            } catch (Throwable ignored) {
            }
            Log.info("bridge disconnected, clients=" + clients.size());
        }
    }

    /** Poll one inbound command line, or null. Game thread only. */
    public static String pollCommand() {
        return inbox.poll();
    }

    public static boolean hasClient() {
        synchronized (clients) {
            return !clients.isEmpty();
        }
    }

    /** Broadcast one NDJSON line to all connected bridges. Never throws. */
    public static void send(String line) {
        ArrayList<Socket> snapshot;
        synchronized (clients) {
            if (clients.isEmpty()) {
                return;
            }
            snapshot = new ArrayList<Socket>(clients);
        }
        byte[] bytes;
        try {
            bytes = (line + "\n").getBytes("UTF-8");
        } catch (Throwable t) {
            return;
        }
        for (Socket s : snapshot) {
            try {
                s.getOutputStream().write(bytes);
                s.getOutputStream().flush();
            } catch (Throwable t) {
                removeClient(s);
            }
        }
    }

    private static void sleep(long ms) {
        try {
            Thread.sleep(ms);
        } catch (InterruptedException ignored) {
            Thread.currentThread().interrupt();
        }
    }
}
