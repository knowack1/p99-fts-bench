//! Listening, accepting, and handing each connection to a tokio task.
//!
//! This is the whole of what the rewrite changed about how the mock is served.
//! The Python sink answered every connection on one asyncio loop on one thread,
//! which is why its own CPU was a documented gate of every run against it: past
//! ~61,000 documents per second on the HTTP half it *was* the constraint, and a
//! level that hit that reported the sink's ceiling wearing the loader's name.
//! Here the accept loop spawns a task per connection and the runtime spreads
//! those tasks over `--tokio-workers` threads, which defaults to every core the
//! machine reports.
//!
//! **A lane per connection.** Every counter the mock keeps is striped, and the
//! stripe a connection adds to is handed out here, at accept, so that the
//! per-document counters are contended only when two connections land on one
//! lane. It is an index, not a thread id: tasks move between workers, and a
//! counter keyed on the worker would have to be re-read on every poll.
use std::net::SocketAddr;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;
use std::time::Duration;

use anyhow::{Context, Result};
use tokio::net::{TcpListener, TcpStream};
use tokio::task::JoinHandle;

use crate::conn;
use crate::cql;
use crate::http_wire::{Answering, Routes};
use crate::tcp;

/// What a connection knows about itself. `local` is the address the client
/// reached this process on, taken from the accepted socket rather than from
/// configuration: the CQL half advertises it as `rpc_address`, and a fixed
/// 127.0.0.1 there would send a loader on another box off to connect to itself.
#[derive(Debug, Clone, Copy)]
pub struct Connection {
    pub lane: usize,
    pub local: SocketAddr,
}

/// A bound port with its accept loop running behind it.
#[derive(Debug)]
pub struct Endpoint {
    address: SocketAddr,
    accepting: JoinHandle<()>,
}

impl Endpoint {
    pub fn address(&self) -> SocketAddr {
        self.address
    }

    pub fn port(&self) -> u16 {
        self.address.port()
    }

    pub fn stop(&self) {
        self.accepting.abort();
    }
}

async fn bind(host: &str, port: u16) -> Result<TcpListener> {
    TcpListener::bind((host, port))
        .await
        .with_context(|| format!("cannot listen on {host}:{port}"))
}

/// The HTTP half of the mock: both the OpenSearch-shaped table and the
/// vector-store-shaped one are served through here.
pub async fn serve_http<R: Routes>(
    host: &str,
    port: u16,
    routes: Arc<R>,
    delay: Duration,
) -> Result<Endpoint> {
    let listener = bind(host, port).await?;
    let address = listener.local_addr()?;
    let lanes = Lanes::default();
    Ok(Endpoint {
        address,
        accepting: tokio::spawn(async move {
            while let Some((stream, connection)) = accept(&listener, &lanes).await {
                let answering = Answering::new(Arc::clone(&routes), &connection);
                tokio::spawn(conn::converse(stream, answering, delay));
            }
        }),
    })
}

pub async fn serve_cql(
    host: &str,
    port: u16,
    node: Arc<cql::Node>,
    delay: Duration,
) -> Result<Endpoint> {
    let listener = bind(host, port).await?;
    let address = listener.local_addr()?;
    let lanes = Lanes::default();
    Ok(Endpoint {
        address,
        accepting: tokio::spawn(async move {
            while let Some((stream, connection)) = accept(&listener, &lanes).await {
                let handler = cql::Handler::new(
                    Arc::clone(&node),
                    connection.local.ip().to_string(),
                    connection.lane,
                );
                tokio::spawn(conn::converse(stream, handler, delay));
            }
        }),
    })
}

/// An accept that fails is not a run that ends: a per-connection error is the
/// client's, and the listener keeps answering. Only a listener that has been
/// closed ends the loop, which is what `Endpoint::stop` does.
///
/// The pause before retrying is for the one error that is not the client's. Out
/// of file descriptors, every `accept` fails at once and forever, and a loop
/// with no pause spins a worker at full tilt for as long as the condition lasts
/// — inside an instrument whose whole claim is that it is not the constraint.
/// A tenth of a second is invisible to a run that is working and bounds the
/// damage of one that is not.
const AFTER_A_FAILED_ACCEPT: Duration = Duration::from_millis(100);

/// The socket options are set here, where the socket is accepted, rather than
/// at each of the two spawn sites: there is one accept path and there should be
/// one place that configures what it returns.
pub(crate) async fn accept(
    listener: &TcpListener,
    lanes: &Lanes,
) -> Option<(TcpStream, Connection)> {
    loop {
        let Ok((stream, _)) = listener.accept().await else {
            tokio::time::sleep(AFTER_A_FAILED_ACCEPT).await;
            continue;
        };
        let Ok(local) = stream.local_addr() else {
            continue;
        };
        tcp::accepted(&stream);
        return Some((
            stream,
            Connection {
                lane: lanes.next(),
                local,
            },
        ));
    }
}

#[derive(Debug, Default)]
pub(crate) struct Lanes(AtomicUsize);

impl Lanes {
    fn next(&self) -> usize {
        self.0.fetch_add(1, Ordering::Relaxed)
    }
}

#[cfg(test)]
#[path = "server_tests.rs"]
mod tests;
