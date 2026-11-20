// Bottom 8 mm of the speaker body for testing the Dayton flange and inserts.
intersection() {
    import("out/body.stl", convexity=10);
    translate([-60,-60,-1]) cube([120,120,9]);
}
