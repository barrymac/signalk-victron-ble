const { spawn } = require('child_process')

const schema = require('./schema')

const pkgData = require('./package.json')

module.exports = function (app) {
  let child
  function sleep(ms) {
    return new Promise((resolve) => {
      setTimeout(resolve, ms);
    });
  }
  function run_python_plugin(options) {
      let args = ['plugin.py']
      child = spawn('ve/bin/python', args, { cwd: __dirname })

      const cleanup = () => {
          if (child) {
              child.removeAllListeners()
              child = undefined
          }
      }

      child.stdout.on('data', data => {
        app.debug(data.toString())
        
        try {
          data.toString().split(/\r?\n/).forEach(line => {
            if (line.length > 0) {
              const delta = JSON.parse(line)
              
              // Debug log engine states explicitly
              delta.updates?.forEach(update => {
                update.values?.forEach(value => {
                  if (value.path.startsWith('propulsion.')) {
                    app.debug(`Engine State Update: ${JSON.stringify(value)}`)
                  }
                })
              })
              
              app.handleMessage(undefined, delta)
              app.handleMessage(pkgData.name, {
                updates: [{
                  values: [{
                    path: "plugins.victronBLE.status",
                    value: "active"
                  }]
                }]
              })
            }
          })
        } catch (e) {
          app.error('Data processing error:', e.message)
        }
      })

      child.stderr.on('data', fromChild => {
        fromChild.toString().split(/\r?\n/).forEach(line => {
          if(line.match(/DEBUG/)) {
            app.debug(line.replace(/^.*?DEBUG\s+/, '').trim())
          } else if(line.match(/INFO/)) {
            app.info(line.replace(/^.*?INFO\s+/, '').trim())
          } else if(line.match(/WARNING/)) {
            app.warn(line.replace(/^.*?WARNING\s+/, '').trim())
          } else if(line.match(/ERROR/)) {
            app.error(line.replace(/^.*?ERROR\s+/, '').trim())
          }
        })
      })

      child.on('error', err => {
        app.error('Subprocess error:', err)
        cleanup()
        setTimeout(() => run_python_plugin(options), 2000)
      })

      child.on('close', code => {
        app.handleMessage(pkgData.name, {
          updates: [{
            values: [{
              path: "plugins.victronBLE.status",
              value: "inactive"
            }]
          }]
        })
        cleanup()
        if (code !== 0) {
          app.warn(`Plugin exited ${code}, restarting in 2s...`)
          setTimeout(() => run_python_plugin(options), 2000)
        }
      })

      child.stdin.write(JSON.stringify({
        adapter: options.adapter || 'hci0',
        devices: options.devices
      }))
      child.stdin.write('\n')
  };
  return {
    start: (options) => {
      run_python_plugin(options)
      return () => {} // Return dummy stop for compatibility
    },
    stop: () => {
      if (child) {
        process.kill(child.pid)
        child = undefined
      }
    },
    schema,
    id: pkgData.name,
    name: "Victron Instant Data over BLE"
  }
}
