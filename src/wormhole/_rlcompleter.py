from __future__ import print_function, unicode_literals
import os, traceback
from sys import stderr
try:
    import readline
except ImportError:
    readline = None
from six.moves import input
from attr import attrs, attrib
from twisted.internet.defer import inlineCallbacks, returnValue
from twisted.internet.threads import deferToThread, blockingCallFromThread

errf = None
if 0:
    errf = open("err", "w") if os.path.exists("err") else None
def debug(*args, **kwargs):
    if errf:
        print(*args, file=errf, **kwargs)
        errf.flush()

@attrs
class CodeInputter(object):
    _input_helper = attrib()
    _reactor = attrib()
    def __attrs_post_init__(self):
        self.used_completion = False
        self._matches = None
        # once we've claimed the nameplate, we can't go back
        self._committed_nameplate = None # or string

    def bcft(self, f, *a, **kw):
        return blockingCallFromThread(self._reactor, f, *a, **kw)

    def completer(self, text, state):
        try:
            return self._wrapped_completer(text, state)
        except Exception as e:
            # completer exceptions are normally silently discarded, which
            # makes debugging challenging
            print("completer exception: %s" % e)
            traceback.print_exc()
            raise e

    def _wrapped_completer(self, text, state):
        self.used_completion = True
        # if we get here, then readline must be active
        ct = readline.get_completion_type()
        if state == 0:
            debug("completer starting (%s) (state=0) (ct=%d)" % (text, ct))
            self._matches = self._commit_and_build_completions(text)
            debug(" matches:", " ".join(["'%s'" % m for m in self._matches]))
        else:
            debug(" s%d t'%s' ct=%d" % (state, text, ct))

        if state >= len(self._matches):
            debug("  returning None")
            return None
        debug("  returning '%s'" % self._matches[state])
        return self._matches[state]

    def _commit_and_build_completions(self, text):
        ih = self._input_helper
        if "-" in text:
            got_nameplate = True
            nameplate, words = text.split("-", 1)
        else:
            got_nameplate = False
            nameplate = text # partial

        # 'text' is one of these categories:
        #  "": complete on nameplates
        #  "12" (multiple matches): complete on nameplates
        #  "123" (single match): return "123-" (no commit, no refresh)
        #   nope: need to let readline add letters
        #   so: return "123-"
        #  "123-": commit to nameplate (if not already), complete on words

        if self._committed_nameplate:
            if not got_nameplate or nameplate != self._committed_nameplate:
                # they deleted past the committment point: we can't use
                # this. For now, bail, but in the future let's find a
                # gentler way to encourage them to not do that.
                raise ValueError("nameplate (NN-) already entered, cannot go back")
        if not got_nameplate:
            # we're completing on nameplates: "" or "12" or "123"
            self.bcft(ih.refresh_nameplates) # results arrive later
            debug("  getting nameplates")
            completions = self.bcft(ih.get_nameplate_completions, nameplate)
        else: # "123-"
            # time to commit to this nameplate, if they haven't already
            if not self._committed_nameplate:
                debug("  choose_nameplate(%s)" % nameplate)
                self.bcft(ih.choose_nameplate, nameplate)
                self._committed_nameplate = nameplate
            # and we're completing on words now
            debug("  getting words")
            completions = self.bcft(ih.get_word_completions, words)

        # rlcompleter wants full strings
        return sorted([text+c for c in completions])

    def finish(self, text):
        if "-" not in text:
            raise ValueError("incomplete wormhole code")
        nameplate, words = text.split("-", 1)

        if self._committed_nameplate:
            if nameplate != self._committed_nameplate:
                # they deleted past the committment point: we can't use
                # this. For now, bail, but in the future let's find a
                # gentler way to encourage them to not do that.
                raise ValueError("nameplate (NN-) already entered, cannot go back")
        else:
            debug("  choose_nameplate(%s)" % nameplate)
            self._input_helper.choose_nameplate(nameplate)
        debug("  choose_words(%s)" % words)
        self._input_helper.choose_words(words)

def _input_code_with_completion(prompt, input_helper, reactor):
    c = CodeInputter(input_helper, reactor)
    if readline is not None:
        if readline.__doc__ and "libedit" in readline.__doc__:
            readline.parse_and_bind("bind ^I rl_complete")
        else:
            readline.parse_and_bind("tab: complete")
        readline.set_completer(c.completer)
        readline.set_completer_delims("")
        debug("==== readline-based completion is prepared")
    else:
        debug("==== unable to import readline, disabling completion")
        pass
    code = input(prompt)
    # Code is str(bytes) on py2, and str(unicode) on py3. We want unicode.
    if isinstance(code, bytes):
        code = code.decode("utf-8")
    c.finish(code)
    return c.used_completion

def warn_readline():
    # When our process receives a SIGINT, Twisted's SIGINT handler will
    # stop the reactor and wait for all threads to terminate before the
    # process exits. However, if we were waiting for
    # input_code_with_completion() when SIGINT happened, the readline
    # thread will be blocked waiting for something on stdin. Trick the
    # user into satisfying the blocking read so we can exit.
    print("\nCommand interrupted: please press Return to quit", file=stderr)

    # Other potential approaches to this problem:
    # * hard-terminate our process with os._exit(1), but make sure the
    #   tty gets reset to a normal mode ("cooked"?) first, so that the
    #   next shell command the user types is echoed correctly
    # * track down the thread (t.p.threadable.getThreadID from inside the
    #   thread), get a cffi binding to pthread_kill, deliver SIGINT to it
    # * allocate a pty pair (pty.openpty), replace sys.stdin with the
    #   slave, build a pty bridge that copies bytes (and other PTY
    #   things) from the real stdin to the master, then close the slave
    #   at shutdown, so readline sees EOF
    # * write tab-completion and basic editing (TTY raw mode,
    #   backspace-is-erase) without readline, probably with curses or
    #   twisted.conch.insults
    # * write a separate program to get codes (maybe just "wormhole
    #   --internal-get-code"), run it as a subprocess, let it inherit
    #   stdin/stdout, send it SIGINT when we receive SIGINT ourselves. It
    #   needs an RPC mechanism (over some extra file descriptors) to ask
    #   us to fetch the current nameplate_id list.
    #
    # Note that hard-terminating our process with os.kill(os.getpid(),
    # signal.SIGKILL), or SIGTERM, doesn't seem to work: the thread
    # doesn't see the signal, and we must still wait for stdin to make
    # readline finish.

@inlineCallbacks
def input_with_completion(prompt, input_helper, reactor):
    t = reactor.addSystemEventTrigger("before", "shutdown", warn_readline)
    #input_helper.refresh_nameplates()
    used_completion = yield deferToThread(_input_code_with_completion,
                                          prompt, input_helper, reactor)
    reactor.removeSystemEventTrigger(t)
    returnValue(used_completion)
