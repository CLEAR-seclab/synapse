import shlex
import logging
import argparse
import collections

import synapse.exc as s_exc
import synapse.common as s_common

import synapse.lib.node as s_node
import synapse.lib.cache as s_cache
import synapse.lib.types as s_types

logger = logging.getLogger(__name__)

class Runtime:
    '''
    A Runtime represents the instance of a running query.
    '''
    def __init__(self, snap, opts=None, user=None):

        if opts is None:
            opts = {}

        self.vars = {}
        self.opts = opts
        self.snap = snap
        self.user = user

        self.inputs = []    # [synapse.lib.node.Node(), ...]

        self.iden = s_common.guid()

        varz = self.opts.get('vars')
        if varz is not None:
            self.vars.update(varz)

        self.canceled = False
        self.elevated = False

        # used by the digraph projection logic
        self._graph_done = {}
        self._graph_want = collections.deque()

    def printf(self, mesg):
        return self.snap.printf(mesg)

    def warn(self, mesg, **info):
        return self.snap.warn(mesg, **info)

    def elevate(self):

        if self.user is not None:
            if not self.user.admin:
                raise s_exc.AuthDeny(mesg='user is not admin', user=self.user.name)

        self.elevated = True

    def tick(self):

        if self.canceled:
            raise s_exc.Canceled()

    def cancel(self):
        self.canceled = True

    def initPath(self, node):
        return s_node.Path(self, dict(self.vars), [node])

    def getOpt(self, name, defval=None):
        return self.opts.get(name, defval)

    def setOpt(self, name, valu):
        self.opts[name] = valu

    def addInput(self, node):
        '''
        Add a Node() object as input to the query runtime.
        '''
        self.inputs.append(node)

    def getInput(self):

        for node in self.inputs:
            yield node, self.initPath(node)

        for ndef in self.opts.get('ndefs', ()):

            node = self.snap.getNodeByNdef(ndef)
            if node is not None:
                yield node, self.initPath(node)

        for iden in self.opts.get('idens', ()):

            buid = s_common.uhex(iden)

            node = self.snap.getNodeByBuid(buid)
            if node is not None:
                yield node, self.initPath(node)

    @s_cache.memoize(size=100)
    def allowed(self, *args):

        # a user will be set by auth subsystem if enabled
        if self.user is None:
            return

        if self.elevated:
            return

        if self.user.allowed(args, elev=False):
            return

        # fails will not be cached...
        perm = '.'.join(args)
        raise s_exc.AuthDeny(perm=perm, user=self.user.name)

    def execStormQuery(self, query):
        count = 0
        for node, path in self.iterStormQuery(query):
            count += 1
        return count

    def iterStormQuery(self, query):
        # init any options from the query
        # (but dont override our own opts)
        for name, valu in query.opts.items():
            self.opts.setdefault(name, valu)

        for node, path in query.iterNodePaths(self):
            self.tick()
            yield node, path

class Parser(argparse.ArgumentParser):

    def __init__(self, prog=None, descr=None):

        self.printf = None
        self.exited = False

        argparse.ArgumentParser.__init__(self,
            prog=prog,
            description=descr,
            formatter_class=argparse.RawDescriptionHelpFormatter)

    def exit(self, status=0, message=None):
        '''
        Argparse expects exit() to be a terminal function and not return.
        As such, this function must raise an exception which will be caught
        by Cmd.hasValidOpts.
        '''
        if message:
            self._print_message(message)
        self.exited = True
        raise s_exc.BadSyntaxError(mesg=message, prog=self.prog)

    def _print_message(self, text, fd=None):

        if self.printf is None:
            return

        for line in text.split('\n'):
            self.printf(line)

class Cmd:
    '''
    A one line description of the command.

    Command usage details and long form description.

    Example:

        cmd --help
    '''
    name = 'cmd'

    def __init__(self, text):
        self.opts = None
        self.text = text
        self.argv = self.getCmdArgv()
        self.pars = self.getArgParser()

    @classmethod
    def getCmdBrief(cls):
        return cls.__doc__.strip().split('\n')[0]

    def getCmdArgv(self):
        return shlex.split(self.text)

    def getArgParser(self):
        return Parser(prog=self.name, descr=self.__class__.__doc__)

    def hasValidOpts(self, snap):
        self.pars.printf = snap.printf
        try:
            self.opts = self.pars.parse_args(self.argv)
        except s_exc.BadSyntaxError as e:
            pass
        return not self.pars.exited

    def execStormCmd(self, runt, genr):
        # override me!
        yield from self.runStormCmd(runt.snap, genr)

    def runStormCmd(self, snap, genr):
        # Older API.  Prefer execStormCmd().
        yield from genr

class HelpCmd(Cmd):
    '''
    List available commands and a brief description for each.
    '''
    name = 'help'

    def getArgParser(self):
        pars = Cmd.getArgParser(self)
        pars.add_argument('command', nargs='?', help='Show the help output for a given command.')
        return pars

    def runStormCmd(self, snap, genr):

        yield from genr

        if not self.opts.command:
            for name, ctor in sorted(snap.core.getStormCmds()):
                snap.printf('%.20s: %s' % (name, ctor.getCmdBrief()))

        snap.printf('')
        snap.printf('For detailed help on any command, use <cmd> --help')

class LimitCmd(Cmd):
    '''
    Limit the number of nodes generated by the query in the given position.

    Example:

        inet:ipv4 | limit 10
    '''

    name = 'limit'

    def getArgParser(self):
        pars = Cmd.getArgParser(self)
        pars.add_argument('count', type=int, help='The maximum number of nodes to yield.')
        return pars

    def runStormCmd(self, snap, genr):

        for count, item in enumerate(genr):

            if count >= self.opts.count:
                snap.printf(f'limit reached: {self.opts.count}')
                break

            yield item

class UniqCmd(Cmd):
    '''
    Filter nodes by their uniq iden values.
    When this is used a Storm pipeline, only the first instance of a
    given node is allowed through the pipeline.

    Examples:

        #badstuff +inet:ipv4 ->* | uniq

    '''

    name = 'uniq'

    def getArgParser(self):
        pars = Cmd.getArgParser(self)
        return pars

    def runStormCmd(self, snap, genr):
        buidset = set()
        for node, path in genr:
            if node.buid in buidset:
                continue
            buidset.add(node.buid)
            yield node, path

class DelNodeCmd(Cmd):
    '''
    Delete nodes produced by the previous query logic.

    (no nodes are returned)

    Example

        inet:fqdn=vertex.link | delnode
    '''
    name = 'delnode'

    def getArgParser(self):
        pars = Cmd.getArgParser(self)
        forcehelp = 'Force delete even if it causes broken references (requires admin).'
        pars.add_argument('--force', default=False, action='store_true', help=forcehelp)
        return pars

    def execStormCmd(self, runt, genr):

        if self.opts.force:
            if runt.user is not None and not runt.user.admin:
                mesg = '--force requires admin privs.'
                raise s_exc.AuthDeny(mesg=mesg)

        for node, path in genr:

            # make sure we can delete the tags...
            for tag in node.tags.keys():
                runt.allowed('tag:del', *tag.split('.'))

            runt.allowed('node:del', node.form.name)

            node.delete(force=self.opts.force)

        # a bit odd, but we need to be detected as a generator
        yield from ()

class SudoCmd(Cmd):
    '''
    Use admin priviliges to bypass standard query permissions.

    Example:

        sudo | [ inet:fqdn=vertex.link ]
    '''
    name = 'sudo'

    def execStormCmd(self, runt, genr):
        runt.elevate()
        yield from genr

# TODO
#class AddNodeCmd(Cmd):     # addnode inet:ipv4 1.2.3.4 5.6.7.8
#class DelPropCmd(Cmd):     # | delprop baz
#class SetPropCmd(Cmd):     # | setprop foo bar
#class AddTagCmd(Cmd):      # | addtag --time 2015 #hehe.haha
#class DelTagCmd(Cmd):      # | deltag #foo.bar
#class SeenCmd(Cmd):        # | seen --from <guid>update .seen and seen=(src,node).seen
#class SourcesCmd(Cmd):     # | sources ( <nodes> -> seen:ndef :source -> source )

class ReIndexCmd(Cmd):
    '''
    Use admin priviliges to re index/normalize node properties.

    Example:

        foo:bar | reindex --subs

        reindex --type inet:ipv4

    NOTE: This is mostly for model updates and migrations.
          Use with caution and be very sure of what you are doing.
    '''
    name = 'reindex'

    def getArgParser(self):
        pars = Cmd.getArgParser(self)
        pars.add_argument('--type', default=None, help='Re-index all properties of a specified type.')
        pars.add_argument('--subs', default=False, action='store_true', help='Re-parse and set sub props.')
        return pars

    def runStormCmd(self, snap, genr):

        if snap.user is not None and not snap.user.admin:
            snap.warn('reindex requires an admin')
            return

        snap.elevated = True
        snap.writeable()

        # are we re-indexing a type?
        if self.opts.type is not None:

            # is the type also a form?
            form = snap.model.forms.get(self.opts.type)

            if form is not None:

                snap.printf(f'reindex form: {form.name}')
                for buid, norm in snap.xact.iterFormRows(form.name):
                    snap.stor(form.getSetOps(buid, norm))

            for prop in snap.model.getPropsByType(self.opts.type):

                snap.printf(f'reindex prop: {prop.full}')

                formname = prop.form.name

                for buid, norm in snap.xact.iterPropRows(formname, prop.name):
                    snap.stor(prop.getSetOps(buid, norm))

            return

        for node, path in genr:

            form, valu = node.ndef
            norm, info = node.form.type.norm(valu)

            subs = info.get('subs')
            if subs is not None:
                for subn, subv in subs.items():
                    if node.form.props.get(subn):
                        node.set(subn, subv)

            yield node, path

class MoveTagCmd(Cmd):
    '''
    Rename an entire tag tree and preserve time intervals.

    Example:

        movetag #foo.bar #baz.faz.bar
    '''
    name = 'movetag'

    def getArgParser(self):
        pars = Cmd.getArgParser(self)
        pars.add_argument('oldtag', help='The tag tree to rename.')
        pars.add_argument('newtag', help='The new tag tree name.')
        return pars

    def runStormCmd(self, snap, genr):

        oldt = snap.addNode('syn:tag', self.opts.oldtag)
        oldstr = oldt.ndef[1]
        oldsize = len(oldstr)

        newt = snap.addNode('syn:tag', self.opts.newtag)
        newstr = newt.ndef[1]

        retag = {oldstr: newstr}

        # first we set all the syn:tag:isnow props
        for node in snap.getNodesBy('syn:tag', self.opts.oldtag, cmpr='^='):

            tagstr = node.ndef[1]
            if tagstr == oldstr: # special case for exact match
                node.set('isnow', newstr)
                continue

            newtag = newstr + tagstr[oldsize:]

            newnode = snap.addNode('syn:tag', newtag)

            olddoc = node.get('doc')
            if olddoc is not None:
                newnode.set('doc', olddoc)

            oldtitle = node.get('title')
            if oldtitle is not None:
                newnode.set('title', oldtitle)

            retag[tagstr] = newtag
            node.set('isnow', newtag)

        # now we re-tag all the nodes...
        count = 0
        for node in snap.getNodesBy(f'#{oldstr}'):

            count += 1

            tags = list(node.tags.items())
            tags.sort(reverse=True)

            for name, valu in tags:

                newt = retag.get(name)
                if newt is None:
                    continue

                node.delTag(name)
                node.addTag(newt, valu=valu)

        snap.printf(f'moved tags on {count} nodes.')

        for node, path in genr:
            yield node, path

class SpinCmd(Cmd):
    '''
    Iterate through all query results, but do not yield any.
    This can be used to operate on many nodes without returning any.

    Example:

        foo:bar:size=20 [ +#hehe ] | spin

    '''
    name = 'spin'

    def runStormCmd(self, snap, genr):

        yield from ()

        for node, path in genr:
            pass

class CountCmd(Cmd):
    '''
    Iterate through query results, and print the resulting number of nodes
    which were lifted. This does yield the nodes counted.

    Example:

        foo:bar:size=20 | count

    '''
    name = 'count'

    def runStormCmd(self, snap, genr):

        i = 0
        for i, (node, path) in enumerate(genr, 1):
            yield node, path

        snap.printf(f'Counted {i} nodes.')

class IdenCmd(Cmd):
    '''
    Lift nodes by iden.

    Example:

        iden b25bc9eec7e159dce879f9ec85fb791f83b505ac55b346fcb64c3c51e98d1175 | count
    '''
    name = 'iden'

    def getArgParser(self):
        pars = Cmd.getArgParser(self)
        pars.add_argument('iden', nargs='*', type=str, default=[],
                          help='Iden to lift nodes by. May be specified multiple times.')
        return pars

    def execStormCmd(self, runt, genr):

        yield from genr

        for iden in self.opts.iden:
            try:
                buid = s_common.uhex(iden)
            except Exception as e:
                runt.warn(f'Failed to decode iden: [{iden}]')
                continue

            node = runt.snap.getNodeByBuid(buid)
            if node is not None:
                yield node, runt.initPath(node)

class NoderefsCmd(Cmd):
    '''
    Get nodes adjacent to inbound nodes, up to n degrees away.

    Examples:
        The following examples show long-form options. Short form options exist and
        should be easier for regular use.

        Get all nodes 1 degree away from a input node:

            ask inet:ipv4=1.2.3.4 | noderefs

        Get all nodes 1 degree away from a input node and include the source node:

            ask inet:ipv4=1.2.3.4 | noderefs --join

        Get all nodes 3 degrees away from a input node and include the source node:

            ask inet:ipv4=1.2.3.4 | noderefs --join --degrees 3

        Do not include nodes of a given form in the output or traverse across them:

            ask inet:ipv4=1.2.3.4 | noderefs --omit-form inet:dns:a

        Do not traverse across nodes of a given form (but include them in the output):

            ask inet:ipv4=1.2.3.4 | noderefs --omit-traversal-form inet:dns:a

        Do not include nodes with a specific tag in the output or traverse across them:

            ask inet:ipv4=1.2.3.4 | noderefs --omit-tag omit.nopiv

        Do not traverse across nodes with a sepcific tag (but include them in the output):

            ask inet:ipv4=1.2.3.4 | noderefs --omit-traversal-tag omit.nopiv

        Accept multiple inbound nodes, and unique the output set of nodes across all input nodes:

            ask inet:ipv4=1.2.3.4 inet:ipv4=1.2.3.5 | noderefs --degrees 4 --unique

    '''
    name = 'noderefs'
    def getArgParser(self):
        pars = Cmd.getArgParser(self)
        pars.add_argument('-d', '--degrees', type=int, default=1, action='store',
                          help='Number of degrees to traverse from the source node.')
        pars.add_argument('-te', '--traverse-edge', default=False, action='store_true',
                          help='Traverse Edge type nodes, if encountered, to '
                               'the opposite side of them, if the opposite '
                               'side has not yet been encountered.')
        pars.add_argument('-j', '--join', default=False, action='store_true',
                          help='Include source nodes in the output of the refs command.')
        pars.add_argument('-otf', '--omit-traversal-form', action='append', default=[], type=str,
                          help='Form to omit traversal of. Nodes of forms will still be the output.')
        pars.add_argument('-ott', '--omit-traversal-tag', action='append', default=[], type=str,
                          help='Tags to omit traversal of. Nodes with these '
                               'tags will still be in the output.')
        pars.add_argument('-of', '--omit-form', action='append', default=[], type=str,
                          help='Forms which will not be included in the '
                               'output or traversed.')
        pars.add_argument('-ot', '--omit-tag', action='append', default=[], type=str,
                          help='Forms which have these tags will not not be '
                               'included in the output or traversed.')
        pars.add_argument('-u', '--unique', action='store_true', default=False,
                          help='Unique the output across ALL input nodes, instead of each input node at a time.')
        return pars

    def runStormCmd(self, snap, genr):

        self.snap = snap
        self.omit_traversal_forms = set(self.opts.omit_traversal_form)
        self.omit_traversal_tags = set(self.opts.omit_traversal_tag)
        self.omit_forms = set(self.opts.omit_form)
        self.omit_tags = set(self.opts.omit_tag)
        self.ndef_props = [prop for prop in self.snap.model.props.values()
                           if isinstance(prop.type, s_types.Ndef)]

        if self.opts.degrees < 1:
            raise s_exc.BadOperArg(mesg='degrees must be greater than or equal to 1', arg='degrees')

        visited = set()

        for node, path in genr:
            if self.opts.join:
                yield node, path

            if self.opts.unique is False:
                visited = set()

            # Don't revisit the inbound node from genr
            visited.add(node.buid)

            yield from self.doRefs(node, path, visited)

    def doRefs(self, srcnode, srcpath, visited):

        srcqueue = collections.deque()
        srcqueue.append((srcnode, srcpath))

        degrees = self.opts.degrees

        while degrees:
            # Decrement degrees
            degrees = degrees - 1
            newqueue = collections.deque()
            while True:
                try:
                    snode, spath = srcqueue.pop()
                except IndexError as e:
                    # We've exhausted srcqueue, loop back around
                    srcqueue = newqueue
                    break

                for pnode, ppath in self.getRefs(snode, spath):
                    if pnode.buid in visited:
                        continue
                    visited.add(pnode.buid)
                    # Are we clear to yield this node?
                    if pnode.ndef[0] in self.omit_forms:
                        continue
                    if self.omit_tags.intersection(set(pnode.tags.keys())):
                        continue

                    yield  pnode, ppath

                    # Can we traverse across this node?
                    if pnode.ndef[0] in self.omit_traversal_forms:
                        continue
                    if self.omit_traversal_tags.intersection(set(pnode.tags.keys())):
                        continue
                    # We're clear to circle back around to revisit nodes
                    # pointed by this node.
                    newqueue.append((pnode, ppath))

    def getRefs(self, srcnode, srcpath):

        # Pivot out to secondary properties which are forms.
        for name, valu in srcnode.props.items():
            prop = srcnode.form.props.get(name)
            if prop is None:  # pragma: no cover
                # this should be impossible
                logger.warning(f'node prop is not form prop: {srcnode.form.name} {name}')
                continue

            if isinstance(prop.type, s_types.Ndef):
                pivo = self.snap.getNodeByNdef(valu)
                if pivo is None:
                    continue  # pragma: no cover
                yield pivo, srcpath.fork(pivo)
                continue

            if isinstance(prop.type, s_types.NodeProp):
                qprop, qvalu = valu
                for pivo in self.snap.getNodesBy(qprop, qvalu):
                    yield pivo, srcpath.fork(pivo)

            form = self.snap.model.forms.get(prop.type.name)
            if form is None:
                continue

            pivo = self.snap.getNodeByNdef((form.name, valu))
            if pivo is None:
                continue  # pragma: no cover

            yield pivo, srcpath.fork(pivo)

        # Pivot in - pick up nodes who have secondary properties who have the same
        # type as me!
        name, valu = srcnode.ndef
        for prop in self.snap.model.propsbytype.get(name, ()):
            for pivo in self.snap.getNodesBy(prop.full, valu):
                yield pivo, srcpath.fork(pivo)

        # Pivot to any Ndef properties we haven't pivoted to yet
        for prop in self.ndef_props:
            for pivo in self.snap.getNodesBy(prop.full, srcnode.ndef):
                if self.opts.traverse_edge and isinstance(pivo.form.type, s_types.Edge):
                    # Determine if srcnode.ndef is n1 or n2, and pivot to the other side
                    if srcnode.ndef == pivo.get('n1'):
                        npivo = self.snap.getNodeByNdef(pivo.get('n2'))
                        if npivo is None:  # pragma: no cover
                            logger.warning('n2 does not exist for edge? [%s]', pivo.ndef)
                            continue
                        yield npivo, srcpath.fork(npivo)
                        continue
                    if srcnode.ndef == pivo.get('n2'):
                        npivo = self.snap.getNodeByNdef(pivo.get('n1'))
                        if npivo is None:  # pragma: no cover
                            logger.warning('n1 does not exist for edge? [%s]', pivo.ndef)
                            continue
                        yield npivo, srcpath.fork(npivo)
                        continue
                    logger.warning('edge type has no n1/n2 property. [%s]', pivo.ndef)  # pragma: no cover
                    continue  # pragma: no cover

                yield pivo, srcpath.fork(pivo)
