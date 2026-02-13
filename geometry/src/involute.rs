/// Involute curve mathematics for gear tooth profiles.
///
/// The involute of a circle is the curve traced by the end of a taut string
/// unwinding from the circle. For a base circle of radius `rb`, a point on the
/// involute at roll angle `t` (radians) is:
///
///   x = rb * (cos(t) + t * sin(t))
///   y = rb * (sin(t) - t * cos(t))

use std::f64::consts::PI;

/// Involute function: inv(α) = tan(α) - α
#[inline]
pub fn involute_function(angle_rad: f64) -> f64 {
    angle_rad.tan() - angle_rad
}

/// Compute a point on the involute of a circle with given base radius
/// at the specified roll angle (radians).
///
/// Returns (x, y) relative to the circle center.
#[inline]
pub fn involute_point(base_radius: f64, roll_angle: f64) -> (f64, f64) {
    let x = base_radius * (roll_angle.cos() + roll_angle * roll_angle.sin());
    let y = base_radius * (roll_angle.sin() - roll_angle * roll_angle.cos());
    (x, y)
}

/// Compute the roll angle at which the involute reaches a given target radius.
///
/// From the involute geometry: r² = rb² * (1 + t²)
/// So t = sqrt((r/rb)² - 1)
pub fn involute_angle_at_radius(base_radius: f64, target_radius: f64) -> f64 {
    if target_radius <= base_radius {
        return 0.0;
    }
    let ratio = target_radius / base_radius;
    (ratio * ratio - 1.0).sqrt()
}

/// Generate points along the involute curve from `start_radius` to `end_radius`.
///
/// Returns a Vec of [x, y] points suitable for spline fitting.
pub fn involute_curve_points(
    base_radius: f64,
    start_radius: f64,
    end_radius: f64,
    num_points: usize,
) -> Vec<[f64; 2]> {
    if num_points == 0 {
        return vec![];
    }

    let t_start = involute_angle_at_radius(base_radius, start_radius);
    let t_end = involute_angle_at_radius(base_radius, end_radius);

    if num_points == 1 {
        let (x, y) = involute_point(base_radius, t_start);
        return vec![[x, y]];
    }

    let mut points = Vec::with_capacity(num_points);
    for i in 0..num_points {
        let frac = i as f64 / (num_points - 1) as f64;
        let t = t_start + frac * (t_end - t_start);
        let (x, y) = involute_point(base_radius, t);
        points.push([x, y]);
    }
    points
}

/// Rotate a 2D point around the origin by `angle` radians.
#[inline]
pub fn rotate_point(x: f64, y: f64, angle: f64) -> (f64, f64) {
    let cos_a = angle.cos();
    let sin_a = angle.sin();
    (x * cos_a - y * sin_a, x * sin_a + y * cos_a)
}

/// Mirror a 2D point across the X axis.
#[inline]
pub fn mirror_y(x: f64, y: f64) -> (f64, f64) {
    (x, -y)
}

/// Half-tooth angular pitch in radians: π / (2 * z)
/// This is the angular width of one tooth at the pitch circle.
#[inline]
pub fn half_tooth_angle(teeth: u32) -> f64 {
    PI / (2.0 * teeth as f64)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::f64::consts::PI;

    #[test]
    fn test_involute_function_zero() {
        assert!((involute_function(0.0)).abs() < 1e-15);
    }

    #[test]
    fn test_involute_function_known() {
        // inv(20°) = tan(20°) - 20° in radians
        let a = 20.0_f64.to_radians();
        let expected = a.tan() - a;
        assert!((involute_function(a) - expected).abs() < 1e-12);
    }

    #[test]
    fn test_involute_point_at_zero() {
        let (x, y) = involute_point(10.0, 0.0);
        assert!((x - 10.0).abs() < 1e-12);
        assert!(y.abs() < 1e-12);
    }

    #[test]
    fn test_involute_point_radius_increases() {
        let rb = 10.0;
        for t in [0.1, 0.5, 1.0, 1.5] {
            let (x, y) = involute_point(rb, t);
            let r = (x * x + y * y).sqrt();
            // Involute radius should satisfy r² = rb²(1 + t²)
            let expected_r = rb * (1.0 + t * t).sqrt();
            assert!(
                (r - expected_r).abs() < 1e-10,
                "At t={t}: r={r}, expected={expected_r}"
            );
        }
    }

    #[test]
    fn test_angle_at_radius_base() {
        let t = involute_angle_at_radius(10.0, 10.0);
        assert!(t.abs() < 1e-12);
    }

    #[test]
    fn test_angle_at_radius_below_base() {
        let t = involute_angle_at_radius(10.0, 5.0);
        assert!(t.abs() < 1e-12);
    }

    #[test]
    fn test_angle_at_radius_round_trip() {
        let rb = 12.5;
        let target_r = 18.0;
        let t = involute_angle_at_radius(rb, target_r);
        let (x, y) = involute_point(rb, t);
        let actual_r = (x * x + y * y).sqrt();
        assert!(
            (actual_r - target_r).abs() < 1e-10,
            "round trip: actual_r={actual_r}, target={target_r}"
        );
    }

    #[test]
    fn test_curve_points_count() {
        let pts = involute_curve_points(10.0, 10.0, 15.0, 20);
        assert_eq!(pts.len(), 20);
    }

    #[test]
    fn test_curve_points_radii_monotonic() {
        let rb = 10.0;
        let pts = involute_curve_points(rb, 10.0, 15.0, 20);
        let mut prev_r = 0.0;
        for p in &pts {
            let r = (p[0] * p[0] + p[1] * p[1]).sqrt();
            assert!(r >= prev_r - 1e-12, "radius should be monotonically increasing");
            prev_r = r;
        }
    }

    #[test]
    fn test_curve_points_start_end() {
        let rb = 10.0;
        let pts = involute_curve_points(rb, 10.0, 15.0, 50);
        let r_start = (pts[0][0].powi(2) + pts[0][1].powi(2)).sqrt();
        let r_end = (pts.last().unwrap()[0].powi(2) + pts.last().unwrap()[1].powi(2)).sqrt();
        assert!((r_start - 10.0).abs() < 1e-10);
        assert!((r_end - 15.0).abs() < 1e-10);
    }

    #[test]
    fn test_rotate_point() {
        let (x, y) = rotate_point(1.0, 0.0, PI / 2.0);
        assert!(x.abs() < 1e-12);
        assert!((y - 1.0).abs() < 1e-12);
    }

    #[test]
    fn test_mirror_y() {
        let (x, y) = mirror_y(3.0, 5.0);
        assert!((x - 3.0).abs() < 1e-12);
        assert!((y + 5.0).abs() < 1e-12);
    }

    #[test]
    fn test_half_tooth_angle() {
        let a = half_tooth_angle(18);
        let expected = PI / 36.0;
        assert!((a - expected).abs() < 1e-12);
    }
}
