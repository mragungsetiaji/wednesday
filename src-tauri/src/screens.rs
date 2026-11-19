//! Keep a window reachable when a monitor was unplugged (the same rule as `fit_on_screen` in
//! `src/wednesday/workspaces.py`).

use crate::Geometry;

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Rect {
    pub x: i32,
    pub y: i32,
    pub width: u32,
    pub height: u32,
}

const MIN_SIZE: (u32, u32) = (480, 320);

/// If the window's middle isn't on any monitor, move it onto the first one and shrink it to fit.
pub fn fit_on_screen(g: &Geometry, screens: &[Rect]) -> Geometry {
    let mut g = g.clone();
    g.width = g.width.max(MIN_SIZE.0);
    g.height = g.height.max(MIN_SIZE.1);
    let Some(first) = screens.first() else { return g };
    let (cx, cy) = (g.x + g.width as i32 / 2, g.y + g.height as i32 / 2);
    let on = |s: &Rect| s.x <= cx && cx < s.x + s.width as i32 && s.y <= cy && cy < s.y + s.height as i32;
    if screens.iter().any(on) {
        return g;
    }
    g.width = g.width.min(first.width);
    g.height = g.height.min(first.height);
    g.x = first.x + (first.width as i32 - g.width as i32) / 2;
    g.y = first.y + (first.height as i32 - g.height as i32) / 2;
    g
}

#[cfg(test)]
mod tests {
    use super::*;

    fn geo(x: i32, y: i32, width: u32, height: u32) -> Geometry {
        Geometry { route: "#".into(), x, y, width, height, maximized: false }
    }

    #[test]
    fn a_window_on_a_monitor_stays() {
        let screens = [Rect { x: 0, y: 0, width: 1920, height: 1080 }, Rect { x: 1920, y: 0, width: 2560, height: 1440 }];
        let g = fit_on_screen(&geo(2100, 100, 1100, 700), &screens);
        assert_eq!((g.x, g.y, g.width, g.height), (2100, 100, 1100, 700));
    }

    #[test]
    fn a_window_on_an_unplugged_monitor_comes_back_and_fits() {
        let screens = [Rect { x: 0, y: 0, width: 1920, height: 1080 }];
        let g = fit_on_screen(&geo(2100, 100, 2560, 1400), &screens);
        assert_eq!((g.x, g.y, g.width, g.height), (0, 0, 1920, 1080));
        let small = fit_on_screen(&geo(-3000, 50, 1000, 600), &screens);
        assert_eq!((small.x, small.y, small.width, small.height), (460, 240, 1000, 600));
    }

    #[test]
    fn too_small_grows_to_the_minimum() {
        let g = fit_on_screen(&geo(10, 10, 100, 100), &[]);
        assert_eq!((g.width, g.height), (480, 320));
    }
}
