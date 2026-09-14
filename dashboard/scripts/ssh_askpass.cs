using System;
class AskPass {
    static int Main(string[] args) {
        if (!String.Join(" ", args).ToLowerInvariant().Contains("password")) return 1;
        Console.WriteLine(Environment.GetEnvironmentVariable("MD_SSH_PASSWORD") ?? "");
        return 0;
    }
}
