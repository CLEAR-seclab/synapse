import synapse.exc as s_exc
import synapse.cells as s_cells
import synapse.telepath as s_telepath

import synapse.lib.cell as s_cell

import synapse.tests.common as s_test

class EchoAuthApi(s_cell.CellApi):

    def isadmin(self):
        return self.user.admin

    def allowed(self, perm):
        return self.user.allowed(perm)

class EchoAuth(s_cell.Cell):
    cellapi = EchoAuthApi

s_cells.add('echoauth', EchoAuth)

class CellTest(s_test.SynTest):

    def test_cell_auth(self):

        # test out built in cell auth
        with self.getTestDmon(mirror='cellauth') as dmon:

            echo = dmon.shared.get('echo00')
            self.nn(echo)

            host, port = dmon.addr

            url = f'tcp://{host}:{port}/echo00'
            self.raises(s_exc.AuthDeny, s_telepath.openurl, url)

            url = f'tcp://fake@{host}:{port}/echo00'
            self.raises(s_exc.AuthDeny, s_telepath.openurl, url)

            url = f'tcp://root@{host}:{port}/echo00'
            self.raises(s_exc.AuthDeny, s_telepath.openurl, url)

            url = f'tcp://root:newpnewp@{host}:{port}/echo00'
            self.raises(s_exc.AuthDeny, s_telepath.openurl, url)

            url = f'tcp://root:secretsauce@{host}:{port}/echo00'
            with s_telepath.openurl(url) as proxy:
                self.true(proxy.isadmin())
                self.true(proxy.allowed(('hehe', 'haha')))

            user = echo.auth.addUser('visi')
            user.setPasswd('foo')
            user.addRule((True, ('foo', 'bar')))

            url = f'tcp://visi:foo@{host}:{port}/echo00'
            with s_telepath.openurl(url) as proxy:
                self.true(proxy.allowed(('foo', 'bar')))
                self.false(proxy.isadmin())
                self.false(proxy.allowed(('hehe', 'haha')))
